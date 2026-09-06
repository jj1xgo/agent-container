# Phase 6 stage 2 共通broker kernel 設計

## 背景と前提

stage 1（[設計](2026-09-04-broker-kernel-design.md)、6-1〜6-5）は、wire形式・audit行・既存testを変えない純粋なrefactorとしてkernel `src/agent_container/broker/` を抽出し、4 brokerを共通部品と互換adapterの上に載せた（`main` `e2ce4a9`、PR #106まで）。互換性のために各brokerへ残した処理と、kernelがまだ持たない保証は、stage 1設計の「stage 2への継ぎ目」と各「stage 2へ送る」記述に散在している。現状の事実は[調査記録](../plans/2026-09-06-broker-kernel-stage2-investigation.md)に固定した。

本文書はstage 2の設計である。stage 1と異なり、**stage 2は保証と観測可能な挙動を意図的に変える**。変える項目は本文書の表に列挙し、各PRは表の項目番号を引いて既存testとgoldenの更新理由を示す。表に無い挙動変更は行わない。

前提となる現状:

- 6-6（roadmap更新、`CHANGELOG.md` Validation、stage 1実host smoke）は未実施である。
- Issue [#98](https://github.com/jj1xgo/agent-container/issues/98)（`deactivate()` 失敗時のfail-closed cleanup）はopenで、本文書がその設計を担う。
- 実host smoke手順書（`docs/phase2-smoke-test.md`、`docs/phase3-github-broker-smoke-test.md`、`docs/phase4-stabilization-smoke-test.md`、`docs/egress-domain-allowlist-smoke-test.md`、`docs/family-issue-create-broker-smoke-test.md`）はaudit key集合、`jq` allowlist、artifact除去を固定しており、Phase 6の完了条件は「既存の実host smoke手順が変更なしでPASS」である。

## scopeの決定

2026-09-06のbrainstormingで次を決めた。

- **範囲**: kernel保証の強化（fail-closed lifecycle、identity付きcleanup、接続毎peer policy、frame errorの種別化、audit envelope）と、handover／egress／GitHubの3 brokerの完全統一。Familyはpeer policyとcleanupの共通部品に乗せるが、audit transaction、`FamilyRuntimeMount`、自前のaccept loopは保持する。
- **peer policy**: kernelは「peer uidが実行userと一致」を提供し、GitHubへ新規適用する。Familyの祖先chain検証はFamilyだけがpolicyとして渡す。pid登録の一般化と祖先chain検証の全broker適用は後続課題とする。
- **GitHubの失敗表示**: 起動・停止失敗はhandover／egressと同じ固定文（`agentctl` の `error: GitHub broker failed`）へ揃える。
- **順序**: 設計と実装計画は今作る。stage 2の最初のcode PRを `main` へmergeする前に、現行 `main` で6-6を実施してstage 1の証拠を `CHANGELOG.md` とroadmapへ固定する。実装はbranchで先行できる。
- **audit**: 共通keyの必須化のみ。既存key、key順序、timestamp形式は保持し、broker固有keyを `details` へ寄せる案とtimestamp統一は採らない。理由は、Familyの `validate_family_audit` がintake起動・request毎・doctorでaudit file全行を固定schemaで検証するため既存audit fileを持つprojectが起動不能になること、およびsmoke手順書のkey集合記述が変更なしでPASSしなくなることである。

採らなかった案:

- Familyのreadiness gateを「accept前の待機」に変える案。Familyは登録前もacceptしrequest毎に拒否する設計で、agent本体はlauncherのregistration gateで登録完了までexecされない。accept前待機に変えると拒否timingとbacklog挙動が変わるだけで保証は増えない。
- 統一audit envelope（`details`、timestamp統一）。上記のとおりFamily validatorとsmoke手順書に直撃する。Phase 10のObsidian UIが読む形式は、その消費者が決まった時点で設計する。
- `PROTOCOL_VERSION` を2へ上げる案。wire形式を変えないので利点が無く、4定数、egressのliteral 2か所、handover clientのself-check、全wire golden、container imageの同時rollを要する。

## 設計原則

1. kernelはbroker固有moduleを一切importしない（stage 1と同じ）。
2. wire形式、`PROTOCOL_VERSION`、frame golden（`test_broker_frame_golden.py`、`test_broker_egress_golden.py` のframe部、`broker_github_golden.json` のrequest／response／chunk、`broker_family_golden.json` のrequest／response）は不変。
3. audit行のbyteは不変。audit golden（`test_broker_audit_golden.py`、`test_broker_egress_golden.py` のaudit部、GitHub／Family goldenのaudit部）は不変。
4. 既存の実host smoke手順書は変更しない。
5. 失敗messageは固定文で、原因例外は `from None` で切り、private pathや例外文をCLIへ出さない（stage 1のkernel契約と同じ）。
6. 既存testの変更は、本文書の変更項目番号を引いてPR本文に列挙する。番号を引けない変更は振る舞いが意図せず変わった信号なので、止めて報告する。

## kernelの変更

### K1. fail-closed lifecycle（#98）

`SocketBrokerRuntime.stop(join_timeout)` の順序は変えない。`deactivate()` の扱いを次のように定める。

- `deactivate()` が `Exception` を送出したら捕捉し、`deactivate_failed` として記録する。`BaseException`（`KeyboardInterrupt` 等）は素通しする。
- 失効に失敗しても listener close、accept thread join、worker joinは必ず継続する。新規acceptを止め、進行中の接続を回収することが失効失敗時の最低限の封じ込めである。
- `deactivate_after_join=True` の場合も同じ扱いを後段の位置で行う。
- `did_not_stop` なら従来どおり `close()` を呼ばずに `error_type("<label> did not stop")` を送出する。再試行はまず失効から再実行する（`exited` が立っていないため）。
- 失効に失敗していても `close()` を呼び、socket・capability・run directoryの除去を試みて露出面を減らす。ただし失効できていない状態を完了扱いにしないため、`close()` が成功しても `exited` は立てず、`error_type("<label> deactivate failed")` を送出する。再試行は失効から再実行し、`close()` は各brokerの `_cleanup_complete` gateにより冪等である。
- 失敗の報告優先順位は did not stop → deactivate failed → cleanup failed → failed とする。上位が成立したら下位は報告しない（再試行で再評価される）。
- `start()` 失敗時の経路は変えない（listener close → `close()`、`close()` は各broker側で失効を含む）。

`deactivate` 失敗の原因例外は `SocketBrokerRuntime.deactivate_error` に保持し、messageには含めない（`error` と同じ扱い）。

test: `deactivate` に例外を注入する4条件（early／late × inline／thread）で、listener close、worker回収、`close` 呼び出し、message、`exited` が立たないこと、失効が成功する再試行で完了することを固定する。`did_not_stop` と失効失敗が同時のときはdid not stopが報告され、再試行で失効が再実行されることも固定する。

### K2. identity付きcleanup `RuntimeArtifacts`

`remove_runtime_artifacts` を `RuntimeArtifacts` に置き換える。

- `RuntimeArtifacts.open(run_dir, *, label)`: 親directoryを `O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC` で開いてfdを保持し、その `dir_fd` 相対でrun directoryを同じflagで開いてfdを保持する。run directoryは `fstat` で directory・mode `0700`・実行user所有を要求し、dev／inoを捕捉する。Familyの `_run_parent_descriptor`／`_run_descriptor` と同じ形で、S2-3のF2はこの2つのfdへ委譲する。
- `track_file(name)`／`track_socket(name)`: 作成直後にdir_fd経由で `stat(follow_symlinks=False)` し、型（S_ISREG／S_ISSOCK）とdev／inoを捕捉する。Familyのようにcapability fileを持たない構成は `track_file` を呼ばない。
- `track_*` の時点で名前が存在しない場合（bindをmockしたtestなど）はidentity無しとして記録し、`remove()` 時にその名前へ何かが現れていればkernelが作ったものではないので残して失敗とする。
- `remove() -> bool`（Trueで失敗）: 順序は **file → socket → run directory** とする。各名前をdir_fd経由でstatし、`FileNotFoundError` は成功扱い、それ以外の `OSError` は失敗、型またはdev／inoが捕捉値と異なれば **残して失敗**、一致すれば `unlink(name, dir_fd=...)`。run directoryは親の `dir_fd` 相対で `stat(follow_symlinks=False)` し、directory型・dev／ino一致・mode `0700`・実行user所有のときだけ `rmdir(name, dir_fd=...)`。`FileNotFoundError` は成功扱い、その他の `OSError`（差し替えを残した結果の `ENOTEMPTY` を含む）は失敗。
- 成功した `remove()` は両方のfdを閉じ、以後の `remove()` は何もせず `False` を返す（冪等）。失敗した `remove()` は両方のfdを保持し、次の `remove()` は再試行する（handover／egressの「差し替えを取り除いてからcloseし直す」既存契約を保つ）。`close()` は両方のfdだけ閉じる。

stage 1設計の「socket → capability → run directory」という記述は本節の順序に訂正する（実装と6-2計画は当初からcapability → socketであり、handover／egress testが固定している）。

test: 順序、冪等性、各段階の差し替え（regular fileをsocketに、socketをregular fileに、run directoryを別inodeに）で対象を残して失敗を返すこと、`FileNotFoundError` の成功扱い、fdが常に閉じられることを固定する。既存の `remove_runtime_artifacts` testは本APIへ移す。

### K3. 接続毎peer policy

- `Connection` に `peer_pid` と `peer_gid` を追加し、`open_connection` は `SO_PEERCRED` の3値を全て保持する。
- `PeerPolicy` protocol（`admit(connection) -> bool`）と既定実装 `SameUser`（`connection.peer_uid == os.getuid()`）を `broker/peer.py` に置く。
- `admit_connection(client, *, timeout, policy) -> Connection | None` を公開helperとする。`open_connection` の後に `policy.admit` を評価し、拒否なら streamを閉じて `None` を返す（clientのcloseは呼び出し側）。拒否した接続からは1 byteも読まず、応答も書かない。`policy` が `None` なら常に許可する。policyが例外を出した場合はhandler例外と同じくruntimeの `failed` として扱う。
- `SocketBrokerRuntime(peer_policy: PeerPolicy | None = None)` を追加し、`raw_client=False` のとき `_handle_client` が `admit_connection` を使う。既定は `None`（policy無し）で、brokerが明示的に渡す。`raw_client=True` と `peer_policy` の併用は policy が黙って無視される構成なので、`__post_init__` で `ValueError("<label> peer policy is unsupported")` として拒否する。

既定を `SameUser` にしない理由: handoverとegressは `authorize` 内でuidを検査し `authentication` stageのaudit行を書く（`docs/phase2-smoke-test.md` L166がこの行を期待する）。kernelで先に落とすとこのaudit行が消える。

### K4. frame errorの種別

`broker/frame.py` の例外を `FrameError(ValueError)` と4つのsubclass `FrameIncomplete`、`FrameSizeError`、`FrameJsonError`、`FrameSchemaError`、およびstream失敗用 `StreamError(FrameError)` にする。messageは現状のまま（golden・既存test不変）。`read_exact` に `initial_eof: bool = False` を追加し、Trueかつ最初のreadでclean EOFなら `b""` を返す（GitHubのchunk stream用）。

### K5. audit envelope

`AuditLog.append(record)` が共通keyを検証する。`timestamp`（ISO 8601文字列）、`run`（文字列）、`project`（文字列）、`operation`（文字列）、`status`（`ok`／`denied`／`error`）は必須、`stage` は任意の文字列、それ以外のkeyは型を問わず通す。違反は `ValueError("<label> record is invalid")`。key順序とencodeは変えないので既存の行byteは不変。`append_text_record` はGitHubが `AuditLog` に移ることで消費者が無くなるため削除する。

## brokerごとの変更項目

各表の番号をPR本文で引く。「観測挙動」は意図的に変わるもの、「更新するtest」は変更理由を明記して書き換えるもの。

### handover

| # | 変更 | 観測挙動 | 更新するtest |
| --- | --- | --- | --- |
| H1 | `handover_broker_transport.py` の `_read_exact`／`_read_one_request` をkernel `read_frame` に置き換え、`FrameSizeError` → `size`、その他の `FrameError`／`StreamError` → `schema` に写像 | なし（response codeとaudit stageは保存） | `test_handover_broker_transport.py` のstream fake契約が変わる場合のみ、同じ入力で同じcode／stageを検証する形に書き換える |
| H2 | session `close` を `RuntimeArtifacts` に移す | 差し替えられたsocketがS_ISSOCKでも別inodeなら残して失敗する（K2） | `test_handover_broker.py` L176-219 の既存caseは満たす。別inodeのsocket差し替えcaseを追加 |
| H3 | K1の失効失敗契約に乗る | `deactivate` 失敗時に `did not stop`／`deactivate failed` を報告し再試行可能 | 追加のみ |
| H4 | `AuditLog.append` のenvelope検証（K5） | なし | 追加のみ |

handoverの `authorize` によるuid検査とaudit、`peer_policy=None` は現状維持。

### egress

| # | 変更 | 観測挙動 | 更新するtest |
| --- | --- | --- | --- |
| E1 | host側capability file modeを `0400` から `0600` に変更 | run directory内のfile mode | `test_egress_broker.py` L45、`test_broker_egress_golden.py` L133 |
| E2 | `egress_adapter._read_capability` をkernel `read_capability` に、path検証を `validate_exact_path`、socket検証を `validate_socket` に置き換え | `0400`／`0444` を拒否し `0600` のみ受理、実行user所有とsize 44完全一致と `O_NONBLOCK` が加わる、失敗messageは `<label> is invalid` | `test_egress_adapter.py` L131、L157、L165 |
| E3 | `open_gateway_tunnel` を `connect_unix(timeout=30)` に置き換え、接続後に `settimeout(None)` でrelay用blockingへ戻す | 接続待ちが30秒で失敗する | 追加のみ |
| E4 | adapterとruntimeのliteral `1` を `PROTOCOL_VERSION` に置き換え | なし | なし |
| E5 | session `close` を `RuntimeArtifacts` に移す（H2と同じ） | H2と同じ | `test_egress_broker.py` の既存caseは満たす。別inodeのsocket差し替えcaseを追加 |
| E6 | K1／K5に乗る（H3／H4と同じ） | H3と同じ | 追加のみ |

egressの `_handle_client`（`raw_client=True`、tunnel予約、audit stage）は現状維持。

### GitHub

| # | 変更 | 観測挙動 | 更新するtest |
| --- | --- | --- | --- |
| G1 | `UploadPackBrokerRuntime` を `SocketBrokerRuntime`（inline、`raw_client=False`、`client_timeout=30`、`peer_policy=SameUser()`、thread name `github-broker`、`join_timeout=2`）の上に載せ、`BrokerSession.deactivate`（`_closed`／`_capability` をlock内で更新）を `close` から分離 | 起動失敗は `GitHubBrokerRuntimeError("GitHub broker failed to start")`（CLIは `error: GitHub broker failed`）、停止時のcleanup失敗は `cleanup failed`、stop後のhandler例外も `failed` として報告、`did not stop` 時に失効済み、host側30秒timeout、実行user以外のpeerは無応答で切断 | `test_github_broker_compatibility.py` L92-137 を本項の契約へ書き換え、`_thread`／`_stop`／`_error`／`_listener` の直接参照を公開挙動の検証に置き換える |
| G2 | capability生成とfileを `generate_capability`／`create_private_file` に | umaskに依らず `0600`、write失敗messageがkernel固定文 | なし（`test_github_broker.py` L73-80は満たす） |
| G3 | listener bindを `bind_private_listener` に | messageが `broker socket path ...` からkernelの `<label> socket path ...` に | 該当messageを固定するtestは無い |
| G4 | session `close` を `RuntimeArtifacts` に | 順序がcapability → socket → rmdirに、失敗は累積して `cleanup failed`、再試行可能、run directory不在は成功扱い | `test_github_broker.py` L134-138は満たす。順序とfail-fastを固定するtestは無い |
| G5 | audit openerを `AuditLog` に置き換え、`create` 時に `validate()` を呼ぶ | 空のaudit fileが `create` 時に作られる、opener失敗の例外種別がkernel契約（open失敗と非regularは `ValueError`、modeと所有者は `PermissionError`、dev／ino再検証、`O_NONBLOCK`）に | `test_github_broker.py` L254-283（拒否後にaudit fileが無い断言を「空である」に） |
| G6 | request／response codecとstreamをkernelに完全移行。request encodeは `encode_frame`、response decodeは `decode_frame`（typed validationはGitHub側に残しbool versionを拒否）、`_read_exact` は `read_exact(initial_eof=...)`、chunk writeは `write_all` | encode失敗が `TypeError` から `ValueError("broker request is invalid")` に、response decodeのmessageが種別ごと（`frame is incomplete`／`frame size is invalid`／`JSON is invalid`／`schema is invalid`）に、重複keyのmessageが `broker response JSON is invalid` に、`NaN`／`Infinity` を拒否、`version=True` を拒否、list statusは `ValueError`、stream `OSError` は `ValueError("broker stream is invalid")`、short writeを再試行して修復（1 byteずつしか書けないstreamへの `ab` がhex `006100` ではなく4 byte header付きの `00000002616200000000` になる） | `test_github_broker_compatibility.py` L17-90 を本項の契約へ書き換え |
| G7 | container側 `read_broker_capability`／`validate_broker_socket`／`_validate_exact_path` をkernel `read_capability`／`validate_socket`／`validate_exact_path` に | 例外種別とmessageがkernel固定文に、size 44完全一致、open後のidentity再検証。CLI境界（`github_client.py` L341、`git_remote_helper_cli.py` L41）では不可視 | `test_github_broker_transport.py` L876-895 と、`read_broker_capability` をpatchするseam（L272-273、L310-311、L344-345、`github_client.py` 側） |
| G8 | K1／K5に乗る | H3と同じ | 追加のみ |

GitHubのoperation handler、policy、audit record（key、順序、`policy_version`、任意key）、`BrokerRuntimeMount`、`podman.py` は変えない。

### Family

| # | 変更 | 観測挙動 | 更新するtest |
| --- | --- | --- | --- |
| F1 | `handle_family_intake_connection` がkernel `Connection` を受け取り、`SO_PEERCRED` の読み取りをkernel `open_connection` に移す。`FamilyPeerPolicy`（`session.validate_peer(peer_pid, peer_uid)` を包み、`FamilyIntakeDenied` を `False` に変換）を `_serve` が `admit_connection` で適用 | なし（拒否した接続は読まず書かず閉じる、audit無し、capability未消費） | `test_family_intake_transport.py` の入口をConnectionに |
| F2 | `_cleanup_artifacts` を `RuntimeArtifacts`（`track_socket` のみ）へ委譲。`_run_descriptor`／`_run_parent_descriptor` の保持は `RuntimeArtifacts` に移す | なし（差し替えinodeの温存、固定message `family intake cleanup failed`、`did not stop` がcleanup前、descriptorの確実なclose） | `test_family_intake_runtime.py` L264-360 は満たす。fd保持の検証はpatch seamが変われば書き換え |

Familyのaccept loop、consumed後の自己停止、失敗時のclient中断、`check()`、`FamilyRuntimeMount`、audit transactionと `validate_family_audit`、`podman.py` の登録supervisorは変えない。

## 変えないもの

- wire形式、`PROTOCOL_VERSION`、frame golden。
- audit行のbyte、4系統のkey、timestamp形式、audit golden。Familyの `append_family_audit`。
- `StateLayout` のbroker毎3点セット、`FamilyStateLayout`、on-disk path。
- 4つのMount型と `podman.py`。
- Familyのreadiness（request毎のpeer検証という形）。`ReadinessGate` seamは残すが、stage 2で新しい消費者は追加しない。
- smoke手順書。

## PR分割と受け入れ条件

| PR | 内容 | 前提 |
| --- | --- | --- |
| S2-1 | K1〜K5をkernelに追加し、handover（H1〜H4）とegress（E5〜E6）を乗せ替える | mergeの前に現行 `main` で6-6を実施し、stage 1の証拠を `CHANGELOG.md` とroadmapに固定する |
| S2-2 | GitHub G1〜G8 | S2-1 |
| S2-3 | egress E1〜E4、Family F1〜F2 | S2-1 |
| S2-4 | 文書: stage 1設計のcleanup順序訂正、roadmap、`CHANGELOG.md`。stage 2完了gateとして既存smoke手順書を変更なしで再実行し、結果を記録してPhase 6を閉じる | S2-2、S2-3 |

各PRの受け入れ条件:

- 更新する既存testは本文書の項目番号をPR本文に列挙する。番号を引けない変更は止めて報告する。
- frame goldenとaudit goldenは不変（E1のmode断言のみ意図的更新）。
- kernelの新契約にはunit testを追加する（K1の注入4条件、K2の順序・冪等・差し替え、K3の許可・拒否・policy例外、K4の種別、K5の必須key）。
- `bin/lint`、required CI（unit、socket integration 3本、Podman 14件）がPASSする。
- Familyを触るPR（S2-3）はCIに含まれない `tests/integration/test_family_intake_socket.py` と `test_family_forced_unknown.py` をlocalで実行し、結果を記録する。
- 既存brokerのbugを見つけても、本文書の項目に無ければIssueにして別PRとする。

## 検証

1. **kernel unit test。** 上記の新契約。
2. **broker test。** 変更項目に対応する既存testの書き換えと、変更項目に無い挙動の不変。
3. **golden。** frame goldenとaudit goldenが不変であることをPR毎に確認する。
4. **socket integration。** CIの3本と、localのFamily 2本。
5. **stage 1実host smoke（6-6）。** S2-1のmerge前に現行 `main` で既存手順書を変更なしで実行する。
6. **stage 2実host smoke（S2-4）。** 同じ手順書を変更なしで再実行する。roadmapの規則により、これがPASSするまでPhase 6を完了にしない。

## docsとroadmapの更新

- 本文書と同じPRで `docs/development-roadmap.md` の「現在の実施順」をstage 2（本文書、6-6の順序）に更新する。Phase一覧の完了条件は変えない。
- stage 1設計の「stage 2への継ぎ目」2項（cleanup順序）に本文書のK2への参照を付け、「stage 2は別の設計文書で扱う」に本文書へのlinkを付ける。
- `CHANGELOG.md` はcode PRで更新する。

## 後続課題（stage 2に含めない）

- pid登録をruntime共通にし、祖先chain検証を全brokerへ適用する（`podman.py` の常時supervised化を伴う）。
- Familyのaudit transactionをkernel `AuditLog` へ統合し、validatorの二重schemaかmigrationを設計する。
- Phase 10のObsidian UIが読むaudit形式。
- `StateLayout` の畳み込みとMount型の共通protocol。
- `ReadinessGate` の新しい消費者（Phase 8のleaseなど）。

## 実装計画への引き継ぎ

本文書の合意後、writing-plansでS2-1からS2-4のPR毎の実装計画を作る。各PRはTDD（RED→GREEN）で進める。sandboxで `.git/worktrees` が書けない環境ではbranchを `main` checkoutで直接扱い、commit後に `main` へ戻す。
