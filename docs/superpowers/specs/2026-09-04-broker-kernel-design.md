# Phase 6 共通broker kernel 設計

## 背景と目的

agent-containerは、containerの中のagentがhost側の資源へ到達する経路を、用途別のbrokerとして4つ持つ。GitHub App broker、handover broker、egress broker、Family intake brokerである。どれも「host側でUNIX socketを開き、one-time capabilityをfileで渡し、container側clientがcapabilityを添えて4byte length header付きJSON frameを送り、host側が検証して処理し、1行JSONのauditを書く」という同じ構造を持つが、実装は各brokerが個別に行ってきた。

2026-09-04時点の`src/agent_container/`で確認した重複は次の通りである。

- protocol module 4本（`github_broker_protocol`、`handover_broker_protocol`、`egress_broker_protocol`、`family_intake_protocol`）が、4byte big-endian length + JSON objectのcodec、重複key拒否（`_object_without_duplicates`）、定数field拒否（`_reject_constant`）、`_read_exact`、`PROTOCOL_VERSION = 1`、status `ok`／`denied`／`error`を各自で実装している。
- runtime module 4本（`github_broker_runtime`／`github_broker`、`handover_broker_runtime`、`egress_broker_runtime`、`family_intake_runtime`）が、run directory作成、capability生成とmode `0600`のfile、socket path長検証・bind・`chmod 0600`、timeout付きaccept loop、stop、cleanupを各自で持つ。handoverだけが`SO_PEERCRED`でpeer uidを取り、egressだけが接続毎にworker threadを起こし、familyだけがcontainer側runtimeのPID登録gateと失敗時のartifact cleanupを持つ。
- audit書き込み（`_open_audit_file`＋`_write_audit_record`）がhandover、egress、githubの3か所にある。recordのkeyは3系統で異なる。
- container側client 4本（`github_broker_transport`、`handover_broker_client`、`egress_adapter`、`family_intake_client`）が、exact path検証、`O_NOFOLLOW`でのcapability読み取り、socket検証、`AF_UNIX`接続を各自で持つ。
- `StateLayout`はbroker毎に`*_root`／`*_run_root`／`*_audit_file`の3点を追加し続けている。

`docs/development-roadmap.md`はPhase 6（共通control plane）を先に置く理由を「各brokerが個別に重複実装してきた問題を、Phase 8（worktree・task lease）とPhase 9（conversation room）で繰り返さないため」としている。本設計はその重複を、既存4 brokerが合成して使うkernel packageとして解消する。

## scopeの決定

2026-09-04のbrainstormingで次を決めた。

- roadmapのPhase 6定義に含まれていた「project、agent、task、eventのhost側contract」のうち、task・event・agentの新しいdomain contractはPhase 6では**設計しない**。最初の消費者はPhase 8のleaseであり、消費者不在で抽象を固定する危険が大きい。Phase 8の完了条件へ移す。Phase 6は既存4 brokerの共通kernel化に集中する。
- kernel化は**2段階**で行う。stage 1はwire形式、audit行、既存testを変えない純粋なrefactorでkernelを抽出し、4 brokerを乗せ替える。stage 2はFamily intakeが持つ強い保証（readiness gate、fail-closed cleanup）と統一audit schemaをkernelへ入れて全brokerに適用する。Phase 6はstage 1とstage 2の両方が終わって完了とする。本文書はstage 1の設計であり、stage 2は別の設計文書で扱う。
- Phase 8の一部（agent別worktreeの分離だけ）の前倒しは**しない**。worktree分離はlease（誰がどこを書いてよいか）と不可分で、分離だけ入れても同一branchへの競合は防げない。roadmapの「検討する余地」はこの判断で閉じる。
- kernelの形は、project毎・broker毎の4 socketというtopologyを変えない**合成可能なpackage**とする。project毎1 socketへのmultiplex案は、mount、capabilityの粒度、container側clientが全て変わりstage 1の原則に反するうえ、1つの故障で全brokerが止まり、capabilityの最小権限が後退するため採らない。共通helperだけを1 fileに寄せる案は、重複の本体であるruntime lifecycleとauditを解決しないため採らない。

## 構成

新package `src/agent_container/broker/`をkernelとする。**kernelはbroker固有moduleを一切importしない。** 依存は常に`handover_*`／`egress_*`／`github_*`／`family_*` → `broker/`の一方向である。container側clientもkernelを使うため、packageはimageに含める（`.containerignore`は`!src/**`で`src/`全体を許可しており追加設定は不要）。

ただし`tests/container/test_image.py`の`test_effective_image_tree_imports_host_entrypoints_without_host_modules`はtop-levelの`.py`だけを仮image treeへcopyしていたため、6-1でsubdirectoryを含めてcopyするよう修正した。これはbroker testではなくimage contract testのinfra修正である。

6-1の実装と最終reviewで判明した、6-2〜6-5の設計入力を記録する。(1) egressはframing失敗を「egress metadata …」、schema失敗を「egress request／response schema …」と別の接頭辞で報告するため、`FrameSchema`に`label`と別のframe用labelを持たせる追加（既定は`label`）が要る。(2) githubの`decode_response_frame`は長さ0・超過・切り詰め・JSON失敗を全て「broker response frame is invalid」に畳んでおり、`encode_request_frame`は`json.dumps`失敗を包まず`TypeError`を出す。kernel化でmessageと例外型が変わるので、6-4でmessage保存の要否を先に決める。(3) githubの`iter_chunk_stream`はclean EOFで`b""`を返す`read_exact(initial_eof=True)`相当を必要とする。(4) `handover_broker_transport.py`のhost側`_read_exact`は`_RequestFailure("schema")`、長さ検査は`_RequestFailure("size")`を出し、この区別がaudit行に入る。kernelの`read_frame`へ単純に置き換えると`size`が`schema`に畳まれるので、6-2ではruntime側で長さ検査を先に行うか、kernelに例外種別の口を設ける。(5) kernelの`read_capability`はgithubより厳格（前段落参照）。

6-2では`handover_broker_transport.py`を据え置いた。transportのhost側`_read_exact`は`_RequestFailure("schema")`、長さ検査は`_RequestFailure("size")`を出し、この区別がauditの`stage`に入る。kernelの`read_frame`へ単純に置き換えると`size`が`schema`に畳まれてaudit行が変わるため、transportのkernel化は例外種別の口を含めてstage 2で扱う。

6-3ではegressを乗せ替えた。既存test `tests/container/test_egress_broker_runtime.py`は`EgressBrokerRuntime._handle_client(client)`をserve pathの単位として固定し（差し替えた関数がclientを引数に呼ばれること、直接呼ぶと`SO_PEERCRED`が取られること）、egressのfake clientはcontext managerでないため、kernelが先に`Connection`を開く設計ではpeer credentialとstreamが二重になる。そこで「socket本体を要するbrokerも同じhandlerで受ける」という本文書の当初案から外れ、`raw_client=True`をkernelのseamとして追加した。kernelはacceptとworker管理と`client.close()`だけを担い、egressは公開helper `open_connection`で`Connection`を開く。egressのcapability fileはmode `0400`のまま（`create_private_file(mode=...)`）、response frameにはkernelの`MAX_METADATA_BYTES`上限が付くが固定語彙のため到達不能、adapterは前段落のとおり据え置き。golden（`tests/container/test_broker_egress_golden.py`）は`0ca61c2`のencoder／writerで生成した。kernelの`read_exact`はstreamの`OSError`を`ValueError(f"{stream_label} is invalid")`に畳むが、egressのruntime（`_handle_client`）とadapter（`open_gateway_tunnel`）はどちらも`(OSError, ValueError)`を同じ枝で受けるため観測できる挙動は変わらない。githubのtransportは両者を同じ枝で受けないので、6-4ではこの点を先に確認する。

| module | 責務 | 置き換える重複 |
| --- | --- | --- |
| `broker/frame.py` | 4byte big-endian length + JSON objectのcodec。`FrameSchema(label, stream_label, fields, max_bytes, json, frame_label=None)`を受け取り、`encode_frame`／`decode_frame`／`read_frame`／`read_exact`／`write_all`を提供する。`label`はschema／encode error messageの接頭辞、`frame_label`はframing／JSON error messageの接頭辞（`None`なら`label`。egressのように両者の接頭辞が違うbrokerのために6-3で追加）、`stream_label`はstream読み書き失敗時の接頭辞、`fields`はdecode後のkey集合との完全一致要件、`max_bytes`はbody上限、`json`は`JsonOptions(ensure_ascii, allow_nan, sort_keys, separators, encoding)`である。重複key拒否、`NaN`／`Infinity`拒否、非object拒否、size上限、bounded read、partial write retryを1実装にする。protocol version、status集合、code集合、fieldの型検証はbroker毎のpolicyであり、意図的にkernelへ入れず各protocol moduleに残す。githubのchunk stream（`write_chunk_stream`／`iter_chunk_stream`）は6-4でここに置く。 | 4 protocol module |
| `broker/runtime.py` | 2層で構成する。**資源 helper**: `create_private_file(path, body, *, label, mode=0o600)`（`O_CREAT|O_EXCL|O_NOFOLLOW`、`mode`で`open`と`fchmod`、ascii、fsync。egressのcapabilityは`0400`）、`allocate_run_dir(project_root, *, label, attempts=8)`（`token_hex(8)`、mode `0700`、衝突は再試行）、`generate_capability(*, label)`（`token_urlsafe(32)`、`CAPABILITY_PATTERN`検証）、`bind_private_listener(socket_path, *, backlog, label)`（path長107 byte以下、既存path／symlink拒否、bind、`chmod 0600`、listen、失敗時はsocketだけunlink）、`remove_runtime_artifacts(*, capability_path, socket_path, run_dir) -> bool`（capability(S_ISREG)→socket(S_ISSOCK)→rmdirの固定順序、型不一致は残して失敗を返す）。**lifecycle**: `SocketBrokerRuntime(label, thread_name, open_listener, handler, deactivate, close, error_type, readiness=AlwaysReady(), backlog, listener_timeout, client_timeout, concurrency="inline", worker_thread_name="", raw_client=False, deactivate_after_join=False)`。`start()`はlistenerを開きdaemon threadでaccept loopを回す。loopは`stop_event`を見ながら`readiness.wait(listener_timeout)`をpollし、真が返るまでacceptしない。接続毎に公開helper `open_connection(client, *, timeout)`で`settimeout`、`SO_PEERCRED`でpeer uidを取り、`handler(Connection(client, stream, peer_uid))`を呼ぶ（`Connection`はfrozen dataclass）。`raw_client=True`ではConnectionを開かず`handler(client)`にsocket本体を渡し、brokerが自分で`open_connection`を呼ぶ（egress）。`stop(join_timeout=...)`は`stop_event.set → deactivate → listener close → accept thread join → worker join → close`の順（`deactivate_after_join=True`ならdeactivateをworker joinの後、`did not stop`の判定前に呼ぶ）で、`did not stop`（closeせず再試行可能）、`cleanup failed`、handler例外の`failed`を`error_type`で報告する。`deactivate`自体が投げた例外は`error_type`に包まず素通しする（handover 6-2と同じ）。brokerはsession（authorize・audit record・lock）を保持し、`open_listener`／`deactivate`／`close`をcallableで渡す。各brokerは`run_dir`から自分のMount型を作る。`concurrency="thread"`（6-3で追加）は接続毎にdaemon worker thread（`worker_thread_name`、既定は`{thread_name}-worker`）を起こし、workerの`OSError`は接続単位の失敗として握り潰し、それ以外の例外は`error`／`failed`を立てて`stop_event`をsetする。`wait_failed(timeout)`は`failed` eventを待つ（accept loopの失敗でもsetされる）。inline方式のclientは`with client:`、thread方式のclientは`finally: client.close()`で閉じる（それぞれhandover／egressの乗せ替え前の閉じ方）。 | 4 runtime module |
| `broker/audit.py` | `AuditLog(path, *, label)`。`open_descriptor()`は`O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|O_NONBLOCK`で開き、通常file・mode `0600`・実行user所有・`os.stat(follow_symlinks=False)`とのdev／ino一致を検証する（失敗はdescriptorを閉じてから`ValueError`／`PermissionError`）。`validate()`は開いて閉じるだけ、`append(record)`は`json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"`をasciiで追記しfsyncする。stage 1ではrecordのkeyもtimestampの形式も呼び出し側が決める。handoverとegressの実装はlabel以外同一だったので、両方をこの1つで賄う。 | handover／egress／githubの`_open_audit_file`＋`_write_audit_record` |
| `broker/capability.py` | container側。`validate_exact_path(path, *, label)`（絶対path、`resolve(strict=True)`と一致）、`read_capability(path, *, label)`（`O_RDONLY|O_NOFOLLOW|O_NONBLOCK`、通常file、mode `0600`、実行user所有、**size完全一致（44 byte = 43文字 + 改行）**、open後に`resolve`と`lstat`のdev／inoを再検証、ascii、1行）、`validate_socket(path, *, label)`（`S_ISSOCK`、mode `0600`、実行user所有。pathのresolveは呼び出し側が先に行う）、`connect_unix(path, *, timeout, socket_factory)`（接続失敗時はsocketを閉じて再送出）。失敗は全て`ValueError(f"{label} is invalid")`。githubの`read_broker_capability`は`st_size > 45`の上限判定と「statしてからopen」の順序で、kernelより緩い。6-4の承認済み方針では旧readerを残し、厳格化や統一はstage 2で設計する。egressの`egress_adapter._read_capability`も同種で、mode `0400`／`0444`を受理し、owner・size検査と`O_NONBLOCK`を持たず、既存testは`0600`を拒否することを固定している。`open_gateway_tunnel`はtimeoutを設定しないため`connect_unix`にも乗らない。6-3ではadapterを据え置き、6-4ではgithubとともに旧readerを残すと決め、厳格化や統一はstage 2へ送る。 | 4 client module |
| `broker/readiness.py` | `ReadinessGate` protocol（`wait(timeout) -> bool`のみ。Trueで準備完了、Falseで未完了、失敗はraise）と既定実装`AlwaysReady`。runtimeは`stop_event`を見ながら`listener_timeout`間隔で`wait`をpollし、準備完了までacceptしない。stopが先に来れば何もせずに終了する。6-1で想定した`register`／`is_ready`は消費者が無く、familyのPID登録はaccept前のgateではなくrequest毎の`validate_peer`による拒否だと判明したため削除した。familyを乗せ替える6-5では、この差を踏まえてreadiness seamの適用可否を再設計する。 | familyのPID登録gate（移動のみ） |

### 6-4 の承認済み互換性範囲

前述の6-1／6-3の設計入力は当時の記録であり、GitHubの例外保存、clean EOF、capability検証についての現在の決定は本節を優先する。

2026-09-05 のコード調査と利用者承認により、6-4 は互換処理を GitHub 側に残し、完全統一を stage 2 に送る。単純な kernel 置換では JSON の例外型・message、stream の OSError、起動・終了・cleanup の順序、capability と audit opener の検査が変わる。stage 1 の振る舞い保存を優先し、下表の限定範囲を 6-4 の完了条件とする。これは計画上の決定であり、6-4 の実装完了を示さない。

| 6-4 で共通化するもの | GitHub 側に残すもの |
| --- | --- |
| request の header/size/schema 検査。`decode_frame(..., json_decoder=...)` に互換 JSON decoder を渡す | request JSON callback と typed validation、request encoder、response codec。巨大整数など json.loads 自体の ValueError も保存 |
| chunk framing。kernel の `iter_chunk_stream` は `read_bytes(size, initial_eof)` callback を受ける | `_read_exact`、metadata stream readers、raw OSError、clean EOF の扱い。short write の既存挙動を修正しない |
| `accept_clients(listener, *, stop_event)` を kernel と GitHub の loop で利用 | GitHub の start/stop、error 保存、client/stream ownership。timeout や SO_PEERCRED を追加しない |
| 既存 `allocate_run_dir(..., label="broker")` | capability 生成・file作成、listener bind、socket→capability→rmdir の fail-fast cleanup |
| `append_text_record(stream, record)` の JSON→newline→flush→fsync | record policy、旧 TextIO opener と close。既存 `AuditLog` の保証・既定動作は変更しない |

`github_broker_transport.py` と `egress_adapter.py` の capability 検証は据え置く。kernel に緩い検証を既定値として追加しない。stage 2 で完全統一を設計する際に、descriptor identity/owner/size/mode、JSON/stream 例外、partial write、起動失敗・再試行・停止・cleanup、audit opener を明示的な変更項目として比較する。これらの強化や既存 bug の修正は 6-4 の通常 refactor に含めない。

stage 1 の完了は、各 broker の仕様に記録した共通部品と互換 adapter が既存 test/golden/実 host smoke を満たすこととする。「全 broker が同じ lifecycle/capability/audit opener を使う」という当初の全面統一は stage 2 の完了条件へ移す。Phase 6 全体には引き続き両 stage が必要である。

調査: `docs/superpowers/plans/2026-09-05-broker-kernel-6-4-investigation.md`。実装計画: `docs/superpowers/plans/2026-09-05-broker-kernel-6-4-github.md`。golden 基準は `a69bb780dc61f3f0f50c92f668f8686837280f12`。

### stage 1で変えないもの

- **`StateLayout`**。broker毎の`*_root`／`*_run_root`／`*_audit_file`は残す。pathを変えるとstate migrationが要り、振る舞い保存でなくなる。stage 2で`broker_root(name)`のような1関数へ畳むかを検討する。
- **Mount型と`podman.py`**。4つのMount型は中身が異なる。githubは`BrokerRuntimeMount(run_dir, repository)`、handoverは`HandoverRuntimeMount(socket_path, capability_path)`、egressは`EgressRuntimeMount(run_dir, project_id, agent)`に`container_name`、familyは`FamilyRuntimeMount`がfile descriptorを保持し`revalidate()`を持つ状態付きobjectで、`podman.py`は`type(family) is not FamilyRuntimeMount`という完全一致の型gateをかけている。統一は振る舞い保存にならないためstage 1では行わず、`podman.py`は触らない。統一の可否はstage 2で検討する。
- **wire形式**。`PROTOCOL_VERSION`は1のまま。JSON serialization optionsは各brokerの現状を`FrameSchema`に宣言して保存する。実測値は次の通りで、これを揃えるとgolden byte testが落ちる。
  - handover: request／response とも `ensure_ascii=False, allow_nan=False, separators=(",", ":")`、utf-8
  - family: request／response とも `ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True`、utf-8
  - github: request は `ensure_ascii=False, allow_nan=False, separators=(",", ":")` を utf-8、response は `separators=(",", ":"), sort_keys=True` を ascii（`ensure_ascii`・`allow_nan` は既定）
  - egress: request は `allow_nan=False, separators=(",", ":"), sort_keys=True` を ascii、response は `separators=(",", ":"), sort_keys=True` を ascii
  - request と response で options が異なる broker があるため、`FrameSchema` は request 用と response 用を別に宣言する（6-1 で実装済み）。
- **audit行**。keyは3系統のまま（github `bytes, issue_number, operation, policy_version, project, repository, run, status, timestamp`、handover `operation, path, project, run, stage, status, timestamp`、egress `agent, operation, project, run, stage, status, timestamp`、family `operation, project_id, request_id, stage, status, timestamp`）。timestampの形式も現状のまま（github／handover／egressはUTC ISO 8601、familyはepoch秒の整数）。統一はstage 2。

## 乗せ替えの順序とPR分割

kernelを消費者なしで先に作ると机上のAPIになるため、最初のbrokerの乗せ替えと同じPRでkernelを抽出する。以降は1 broker = 1 PRとする。

| PR | 内容 | kernelに入る部品 | 理由 |
| --- | --- | --- | --- |
| 6-1 | `broker/frame.py`と`broker/capability.py`を抽出し、handoverのprotocolとclientをkernel上へ | frame、capability | handoverはoperationが`create`1つで検証しやすい。守るべき追加保証がなく、auditも最も単純 |
| 6-2 | `broker/runtime.py`、`broker/audit.py`、`broker/readiness.py`（`AlwaysReady`のみ）を抽出し、handover runtimeを乗せ替え | runtime（inline）、audit、readiness | handoverは`SO_PEERCRED`を使うので、kernelは最初からpeer credentialを持つ |
| 6-3 | egressを乗せ替え | `FrameSchema.frame_label`、`create_private_file(mode)`、`open_connection`、runtimeの`concurrency="thread"`・worker回収・`wait_failed`・`raw_client`・`deactivate_after_join` | thread方式とtunnel予約という「broker固有の追加状態」をkernelがどう受けるかを、最大のgithubより前に小さいbrokerで決める |
| 6-4 | githubの互換性を保つ部分的乗せ替え | request framing/schema の JSON callback、chunk stream、accept iterator、TextIO audit write。既存 run directory helper を利用 | handler と JSON／lifecycle／cleanup／capability／audit opener の互換処理は残す。全面統一は stage 2（「6-4 の承認済み互換性範囲」参照） |
| 6-5 | familyを乗せ替え | 追加なし。familyの既存PID登録logicはfamily側module（`family_intake_runtime.py`）に`ReadinessGate`の実装として残し、kernel runtimeへ渡す。kernelへは移さない。fail-closed cleanupもfamily側に残す | readiness seamがstage 1で実際の消費者を持つ。「全brokerへ昇格」はstage 2 |
| 6-6 | roadmapの現在地更新、`CHANGELOG.md` Validation、stage 1実host smoke | — | 節「振る舞い保存の証明」の実host gate |

各PRの受け入れ条件は同じである。

- 該当brokerの既存test（protocol、runtime、broker、client、transport）を**変更せずに**passする。変更が必要になったら振る舞いが変わった信号なので、止めて報告し、理由をPR本文に明記して利用者判断とする。
- kernel部品には独自のunit testを追加する（既存testの複製ではなく、kernel固有の境界）。
- `bin/lint`、CIのUnit testsとPodman integrationがpassする。
- 既存brokerのbugを見つけても、kernel化PRには混ぜない。Issueにして別PRとする。

6-1と6-2を分けるのは、frame／capability（pure function）とruntime／audit（OS resourceを持つ）で回帰の性質が違うからである。

## 振る舞い保存の証明

「変えていない」を主張ではなく証拠にするため、3層で固定する。

1. **既存test不変。** PR reviewでは`git diff --stat tests/`に該当brokerの既存test fileが現れないことを確認する。
2. **golden byte test（新規、kernel側）。** 4 brokerそれぞれについて代表的なrequest／response frameを、**kernel化前のencoder（commit hashを明記）で生成した静的byte列としてfixtureにcommit**し、kernel経由のencodeが同一byte列を出すこと、decodeが同じ値へ戻ることを検証する。fixtureはkernelの出力から再生成しない。stage 2でwireを変えるときはこのfixtureを意図的に更新する。
3. **audit行の同一性。** 3系統のauditについて、同じ入力で書かれる1行JSONが`timestamp`以外同一であることを検証する。timestampは注入可能なclockにする（既存brokerが`time`を直接呼んでいる場合、注入に変えることは振る舞いを変えないrefactorの範囲とする）。

kernel固有のunit testは、既存brokerがばらばらに持っていた境界検査を1か所で網羅する。重複key拒否、size上限（ちょうどと+1）、version不一致、非object JSON、不正UTF-8、socket path長超過、既存path／symlinkへのbind拒否、capability fileのmode／所有者／size／複数行、`SO_PEERCRED`のuid不一致、stop後のaccept終了、cleanupの順序と冪等性、thread方式でのworker回収、`ReadinessGate`が満たされるまでacceptしないこと。

**実host smoke（stage 1完了gate）。** roadmapの規則「external-state smokeが未実施ならcodeがmerge済みでもPhaseを完了にしない」に従い、6-6で既存の手順書を**変更せずに**再実行する。`docs/phase3-github-broker-smoke-test.md`（fetch、create-only push、PR、Issue read）、`docs/egress-domain-allowlist-smoke-test.md`、`docs/family-issue-create-broker-smoke-test.md`、handoverは`docs/phase2-smoke-test.md`のCodex／Claude handover create。新しい手順書は書かない。既存手順がそのまま通ることが振る舞い保存の最終証拠であり、結果は`CHANGELOG.md`のValidationとroadmapへ記録する。

## stage 2への継ぎ目

stage 2を後から入れてもstage 1の設計を壊さないよう、kernelの内側に閉じた3つの継ぎ目を用意する。stage 1ではどれも既定値で動く。

1. **readiness gate。** `SocketBrokerRuntime`は`ReadinessGate`を受け取り、`wait`が返るまでacceptしない。既定は`AlwaysReady`。stage 1ではfamilyだけが自分のPID登録実装を渡す。stage 2で「container側runtimeが登録するまで受けない」を全brokerへ広げる。
2. **fail-closed cleanup。** runtimeの`__exit__`と失敗時のcleanupは「socket → capability → run directory」の固定順序で実装し、順序と冪等性をtestで固定する。stage 2でfamilyの`_cleanup_artifacts`（失敗時にも確実に消す、部分残骸の検出）をこの1か所へ持ち込む。
3. **audit envelope。** `AuditLog.append(record)`はstage 1ではrecordをそのまま書く。stage 2で共通key（`timestamp`、`project`、`run`、`operation`、`status`）を必須にし、broker固有keyを`details`へ寄せるか、timestamp形式を統一するかを決める。stage 1では4系統のkey差分を本文書（前節）に一覧化してある。

stage 2で決めること（本文書では決めない）: 6-4で残したJSON／stream例外、lifecycle／cleanup、capability／audit openerの完全統一と保証変更、`PROTOCOL_VERSION`を2へ上げるか、`StateLayout`のbroker毎3点セットを畳むか（state migrationが要る）、Mount型と`podman.py`の統一、統一auditをPhase 10のObsidian UIがどう読むか。

## docsとroadmapの更新

roadmapの更新規則「Phaseを分割、統合、延期、置換するときは同じPRでこの文書を更新する」に従い、本文書と同じPRで`docs/development-roadmap.md`を次のように変える。

- Phase 6の行: 内容を「共通broker kernel」、完了条件を「GitHub／handover／egress／Familyの4 brokerが仕様に記録した共通kernel部品と互換adapter上で動き（stage 1）、残したlifecycle・capability・audit opener等を統一し、kernelがreadiness gate・fail-closed cleanup・統一auditを全brokerへ提供し（stage 2）、既存の実host smoke手順が変更なしでPASSする」とする。
- Phase 8の行: 完了条件へ「task・event・agentのhost側contractをleaseの最初の消費者として定義する」を追加する。
- 「Phase 6〜9の依存理由」節へ、2026-09-04のbrainstormingで「agent別worktreeの前倒しはしない（leaseと不可分）」「task／event contractはPhase 8へ移す」と決めた旨を追記し、「検討する余地は残る」の記述を閉じる。

## やらないこと

- 性能測定、負荷test。
- 新しいoperationやbrokerの追加。
- 既存brokerのbug修正（Issue化して別PR）。
- `StateLayout`、Mount型、`podman.py`、wire形式、audit schemaの変更（stage 2）。
- task／event／agentのdomain contract（Phase 8）。
- agent別worktreeの分離（Phase 8）。

## 実装計画への引き継ぎ

本文書の合意後、writing-plansで6-1から6-6のPR毎の実装計画を作る。各PRはTDD（RED→GREEN）で進め、`.worktrees/`配下のworktreeで作業し、PR本文には既存test不変の証拠（`git diff --stat tests/`）とkernel unit testの追加数を書く。
