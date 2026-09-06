# Phase 6 stage 2 broker kernel: 設計入力の調査記録

Status: 調査のみ。stage 2 の設計文書・実装計画は未作成、利用者承認は未取得。本文書は事実と差分の記録であり、stage 2 の範囲決定を示さない。

## 調査対象の版

- `main` `e2ce4a9`（PR #106 merge、6-5 Family 乗せ替え済み）。stage 1 の 6-1〜6-5 は merge 済み。
- 6-6（roadmap 更新、`CHANGELOG.md` Validation、stage 1 実 host smoke）は**未実施**。`CHANGELOG.md` の 6-4／6-5 Validation はいずれも実 host smoke を `not run — 6-6` と記録している。
- 仕様: `docs/superpowers/specs/2026-09-04-broker-kernel-design.md`。stage 2 で決める事項は同文書「stage 2への継ぎ目」（L145-153）と各「stage 2へ送る」記述（L34、L43-44、L50、L66-68、L78、L101-109、L168）に散在する。
- Issue: [#98](https://github.com/jj1xgo/agent-container/issues/98)（open、`deactivate()` 失敗時の fail-closed cleanup 設計）、[#97](https://github.com/jj1xgo/agent-container/issues/97)（closed、PR #99 で修正済み）。#98 の完了条件は「fail-closed lifecycle の設計に失敗時の契約を明記」「callback failure 注入 test で失効・listener/thread/worker 回収・audit・再試行を検証」「単に try/except で包むだけでは完了としない」。

## 1. kernel の現状（`src/agent_container/broker/`）

| module | 提供 | stage 2 に関わる限界 |
| --- | --- | --- |
| `runtime.py` | `create_private_file`（`fchmod`、ascii、fsync）、`allocate_run_dir`（`token_hex(8)`、8 回）、`generate_capability`、`bind_private_listener`（path ベース、`exists()` 事前検査、path `chmod`）、`remove_runtime_artifacts`（capability(S_ISREG)→socket(S_ISSOCK)→rmdir、型不一致は残して `bool` 返却）、`open_connection`（`SO_PEERCRED` から **uid のみ**保持、pid は捨てる L142）、`accept_clients`、`SocketBrokerRuntime` | cleanup は inode 同一性・owner・mode を検査しない。`stop()` の `deactivate()` は try/except なし（L306、L323）で例外が素通しし、listener close／join／close に進まない（#98）。`start()` 失敗時は `deactivate()` を直接呼ばない（`close()` 経由のみ）。context manager は持たない（consumer 側で提供） |
| `readiness.py` | `ReadinessGate.wait(timeout) -> bool`、`AlwaysReady` | 入力（登録 pid 等）や失敗状態を持たない一回限りの accept 前 poll。test は `AlwaysReady` のみ（`tests/container/test_broker_readiness.py`）。runtime 側の gate 動作は `test_broker_runtime.py` L521-563 で固定（開くまで accept しない、開かずに stop しても収束、raise は `failed`） |
| `audit.py` | `AuditLog`（`O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|O_NONBLOCK`、S_ISREG・0600・uid・dev/ino 再検証、`append` は 1 行 ascii を write loop＋fsync）、`append_text_record`（TextIO 用） | flock、既存内容検証、size 上限、`st_nlink`、親 directory の私有性検査、親 fsync、失敗時 rollback を持たない。key と timestamp 形式は呼び出し側任せ、`sort_keys` なし |
| `capability.py` | `validate_exact_path`、`read_capability`（0600、uid、**size 44 完全一致**、open 後 resolve／lstat 再検証）、`validate_socket`、`connect_unix` | handover client だけが使用。github／egress は独自 reader（§4） |
| `frame.py` | `FrameSchema`、`encode_frame`／`decode_frame`／`read_frame`／`read_exact`／`write_all`、chunk helper | error は `ValueError` の message 接頭辞のみで種別（incomplete／size／JSON／schema）を型で区別できない（handover transport の `size`／`schema` 分離が乗らない理由、§3） |

kernel test は `test_broker_frame.py`、`test_broker_capability.py`、`test_broker_audit.py`、`test_broker_readiness.py`、`test_broker_runtime.py`、`test_broker_worker_start_stop.py`（#97）、`test_broker_github_primitives.py`。`remove_runtime_artifacts` を 2 回呼ぶ冪等性 test と、capability→socket の相対順序を固定する test は無い。`deactivate` が raise する test は tests/ 全体に無い。

仕様 L150 は cleanup 順序を「socket → capability → run directory」と書くが、実装（`runtime.py` L97-116）と 6-2 計画（L109-111、L1186）は capability → socket → rmdir で、GitHub 独自実装は socket → capability（§3）。文書と実装の食い違いは stage 2 の設計で解消する必要がある。

## 2. Family intake が持つ「強い保証」の実体

stage 2 の目標「Family の readiness gate・fail-closed cleanup・audit transaction を kernel へ入れる」に対し、実装は次のとおり。

### 2.1 PID 登録は readiness gate ではなく request 毎の peer 検証

- session は未登録（`_runtime_root is None`）で開始し、listener は登録前から accept する（`family_intake_runtime.py` L299、`family_intake_broker.py` L128、L196-211）。
- 登録は host supervisor `podman.run_command_supervised`（`podman.py` L812-934）が行う。pidfile で container PID を最大 30 秒待ち（L310-333）、`register_runtime(pid)`（L869）成功後に registration gate pipe へ `b"1"` を書く（L870）。container 内 `container/bin/agent-runtime-launcher` L10-21 はこの `b"1"` を読むまで agent を exec しない。よって **agent process 自身は登録前に接続できない**。登録前に接続できるのは host の同一 uid の他 process だけ。
- `validate_peer(pid, uid)`（`family_intake_broker.py` L213-246）は `SO_PEERCRED` の **pid** を使い、登録 root までの祖先 chain（最大 64 hop、cycle 安全）と starttime を検査し、snapshot を再読して TOCTOU を再検査する。登録前・不一致の接続は accept 後に **1 byte も読まず、応答も audit も書かずに閉じる**（`family_intake_transport.py` L29-45、`test_family_intake_transport.py` L202-214 で固定）。
- kernel の `ReadinessGate` は入力を持たない一回限りの poll、`open_connection` は pid を捨てる。Family の保証を seam に載せるには (a) `Connection` に `peer_pid` を追加し、(b) 接続毎の peer policy callback（登録・祖先検査）を kernel に置く、のいずれかまたは両方が要る。accept 前 gate に変えると、登録前接続は「accept して黙って閉じる」から「backlog（8）で待たせる」へ変わり、拒否 timing と `test_family_intake_broker.py` L112 の unarmed 契約が変わる。
- 他 3 broker（github／handover／egress）には登録概念が無い。mount と capability file は container 起動前に揃うため、accept 前 readiness を待つ相手が存在しない。

### 2.2 lifecycle と fail-closed cleanup

- `start()`（`family_intake_runtime.py` L162-271）: binding 読込 → pending store 初期化（audit 全行検証を含む）→ run dir を **dir_fd** で作成し `_run_stat` を捕捉 → socket bind 後に `dir_fd` 経由で stat／chmod／再 stat し `_socket_stat` を捕捉 → `FamilyRuntimeMount.capture`（dir fd と `O_PATH` socket fd を保持）→ thread。失敗時は listener close → `deactivate` → `mount.close` → `_cleanup_artifacts` を行い固定 message `failed to start` を `from None` で送出。
- `_serve`（L297-331）: kernel `accept_clients` 使用。**consumed 後は自分で停止**し、失敗時は listener close と in-flight client の `shutdown(SHUT_RDWR)`（`_fail_runtime` L342-351、`_interrupt_client` L333-340）。integration `test_family_intake_socket.py` L327 は「runtime exit が frame 途中の client を中断する」ことを固定する。kernel `_serve` は例外で `error`／`failed` を立てるだけで listener close も client 中断もしない。
- `close()`（L419-453）: `_stop` → `deactivate` → listener close → client 中断 → join 2 秒 → `did not stop` なら cleanup 前に raise → `_cleanup_artifacts` → `mount.close`。`_cleanup_complete` は **失敗時にも立つ**ため（L415）失敗した close は再試行できない。kernel は `exited` を成功時だけ立てるので再試行可能。
- `_cleanup_artifacts`（L353-417）: socket は `dir_fd` stat が捕捉 inode と一致し S_ISSOCK のときだけ unlink、不一致は残して失敗。run dir は **socket 段階が失敗していなければ**、捕捉 inode と一致し 0700・自 uid のときだけ rmdir。descriptor は常に close。kernel `remove_runtime_artifacts` は path ベースで inode／owner／mode を見ず、socket 段階の失敗後も rmdir を試みる。`test_family_intake_runtime.py` L307、L360 は差し替え inode の温存を固定しており、kernel 版では S_ISSOCK の差し替えを削除してしまう。
- Family には capability file が無く（env `AGENT_FAMILY_CAPABILITY` で渡す）、kernel の `create_private_file`／`remove_runtime_artifacts`／`read_capability` の前提と合わない。`FamilyRuntimeMount` は fd を保持し `revalidate()` を持つ状態付き object で、`podman.py` L237 は `type(...) is FamilyRuntimeMount` の完全一致 gate をかける。

### 2.3 audit transaction（`family_pending.py` L1611-1655）

- 親 directory chain の私有性 walk → `O_RDWR|O_APPEND|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK` で open（`O_CREAT|O_EXCL` 後 fallback、作成時 `fchmod`）→ `st_nlink == 1`・0600・uid・inode 同一 → `flock(LOCK_EX)` → lock 後に再検証 → **既存全行を schema 検証**（4 MiB 上限、末尾改行、固定 6 key、enum）→ write → fsync → identity → **親 fsync** → identity。失敗時は `ftruncate` で rollback。
- record は `operation, project_id, request_id, stage, status, timestamp` 固定 6 key、`sort_keys=True`、`timestamp` は **int epoch 秒**（exact type）。
- `validate_family_audit`（L1535-1576）は **intake runtime の start（`initialize_pending_store`）、`create_pending` 毎、`agentctl family doctor`** で audit file 全体を固定 schema と `project_id` 一致で検証する。したがって Family の audit key／timestamp 形式を変えると、**既存 audit file を持つ project は intake が起動しなくなる**（validator の二重 schema 対応か migration が必須）。
- audit 行は pending record に `audit_event` として埋め込まれ（outbox）、append 成功後に marker を消す at-least-once 設計（`create_pending` L807-832、`_drain_audit_event` L1271-1288）。kernel `AuditLog` にはこの結合が無い。

## 3. GitHub・handover に残した互換処理（6-4／6-2 の据え置き）

6-4 investigation（`2026-09-05-broker-kernel-6-4-investigation.md`、GitHub `1924f71` 基準）の差分表は現 main でも成立する。以降の変更は `allocate_run_dir`、`accept_clients`、`append_text_record`、custom JSON decoder 付き `decode_frame` の採用のみ。同文書の「test は runtime 私有属性を読まない」は現在成立せず、`tests/container/test_github_broker_compatibility.py` L106-137 が `runtime._thread`／`_stop`／`_error`、`session._listener` を直接扱う。仕様 L36 の未確認事項「github transport は OSError と ValueError を同じ枝で受けない」は、現 code では全 handler 枝が `(ValueError, OSError)` を同時に受ける（`github_broker_transport.py` L257 ほか 12 か所）ため解消済み。

kernel `SocketBrokerRuntime`／`AuditLog`／`remove_runtime_artifacts`／`create_private_file` に乗せた場合に変わる観測可能挙動:

| 面 | GitHub 現状 | kernel | 影響先 |
| --- | --- | --- | --- |
| start 失敗 | 元例外（`OSError`／`ValueError`）を素通し。`agentctl` は `error: {error}` または `error: filesystem operation failed` を表示（`agentctl.py` L2141-2146） | `GitHubBrokerRuntimeError("GitHub broker failed to start") from None` → `error: GitHub broker failed`（L2129-2131） | CLI stderr の文言、`test_github_broker_compatibility.py` L92-100 |
| serve | timeout・`SO_PEERCRED` 無し。stop 後の handler 例外は握り潰す（L127-137 で固定） | `open_connection` が 30 秒 timeout と peer uid を追加（`raw_client=True` で回避可）。stop 後の例外も `failed` に載る | 停止時の例外報告 |
| stop | `_stop` → listener close（例外素通し）→ join 2 秒 → `did not stop`（session は open のまま、失効なし）→ `session.close()`（例外素通し）→ `failed` | deactivate → listener close（`cleanup failed` に包む）→ join → close → `exited` | `deactivate` callable が必須（GitHub は `close` 内で `_closed`／`_capability` を同時に処理、L211-212）、error 包み方、冪等性 |
| cleanup | socket → capability の順、型不一致で `ValueError("broker ... changed during cleanup")` を即送出（後続を行わない）、`_closed` を先に立てるため**再試行不可** | capability → socket → rmdir、失敗を累積して `bool`、handover／egress は `_cleanup_complete` gate で再試行可 | 順序、message（test には未固定）、再試行性 |
| capability file | `os.open(..., 0o600)` のみで umask 依存、utf-8 TextIO | `fchmod`、ascii | umask が owner bit を落とす環境のみ |
| audit opener | symlink 事前検査、`O_NONBLOCK` 無し、open 失敗は素の `OSError`、非 regular は `PermissionError`、TextIO を返す、`create` 時に検証せず**遅延作成**（`test_github_broker.py` L283 は拒否後に audit file が無いことを固定） | `O_NONBLOCK`、open 失敗→`ValueError`、非 regular→`ValueError`、dev/ino 再検証、`validate()` で即時作成 | 例外種別、作成 timing |
| protocol | response decode の message 畳み込み、`version=True` 受理、list status で `TypeError`、encode の `TypeError` 素通し、stream の素の `OSError`、chunk short-write bug（hex `006100`）— すべて `test_github_broker_compatibility.py` L17-90 で「現状」として固定 | 種別ごとの message、例外変換、`write_all` で修復 | 互換 test の意図的更新 |
| capability reader（container 側） | stat→open 順、`st_size > 45`、`os.read(46)`、open 後の identity 検査なし | `O_NONBLOCK`、size 44 完全一致、open 後 resolve／lstat 再検証 | CLI 境界では `(ValueError, RuntimeError, OSError)` を同じ枝で受けるため不可視（`github_client.py` L341、`git_remote_helper_cli.py` L41）。unit test の patch seam は変わる |

handover: `handover_broker_transport.py` の `_read_exact`→`_RequestFailure("schema")`、長さ検査→`_RequestFailure("size")` が response code と audit `stage` の両方になる（L133-142）。kernel `read_frame` は種別を message でしか区別しないため乗せていない。runtime・session・audit・cleanup は kernel 上（`SocketBrokerRuntime` inline、`AuditLog`、`remove_runtime_artifacts`）。`deactivate` は lock 内で `_closed`／`_capability` を更新するだけで例外経路は未確認（#98 の記述と同じ）。

## 4. container 側 capability reader の 3 実装

| broker | reader | mode | uid | size | identity 再検証 | timeout |
| --- | --- | --- | --- | --- | --- | --- |
| handover | kernel `read_capability` | 0600 | あり | 44 完全一致 | あり | `connect_unix` |
| github | `github_broker_transport.read_broker_capability` L68-92 | 0600 | あり | `> 45` 拒否 | なし | あり |
| egress | `egress_adapter._read_capability` L106-133 | **0400／0444 のみ**（0600 拒否、`test_egress_adapter.py` L157 で固定） | **なし** | 128 byte 読んで EOF 要求 | dev/ino あり | `open_gateway_tunnel` に **timeout なし** |
| family | env 値、file なし | — | — | — | — | 30 秒 |

host 側の作成 mode は github／handover が 0600、egress が 0400（`egress_broker.py` L31-33、`test_egress_broker.py` L45、golden L133 で固定）。egress を kernel reader に統一するには host 側 mode を 0600 へ変えるか、kernel reader に mode 集合の口を設ける必要がある。egress adapter・runtime の response／request version は `PROTOCOL_VERSION` でなく literal `1`（`egress_adapter.py` L159、`egress_broker_runtime.py` L147）。

## 5. audit の 4 系統と読者

| broker | key（挿入順） | timestamp | 注入 | 書き手 |
| --- | --- | --- | --- | --- |
| github | `timestamp, run, project, repository, operation, status, bytes, policy_version[, ref, pr_number, issue_number, stage]` | ISO 8601 UTC | なし（`datetime` を patch） | 独自 opener＋`append_text_record` |
| handover | `timestamp, run, project, operation, status, stage[, path]` | ISO 8601 UTC | なし | `AuditLog` |
| egress | `timestamp, run, project, agent, operation, status[, stage, bytes_from_client, bytes_from_upstream]` | ISO 8601 UTC | なし | `AuditLog` |
| family | `operation, project_id, request_id, stage, status, timestamp`（sorted） | **int epoch** | あり（`clock`） | `append_family_audit`（§2.3） |

読者:

- src 内で audit 行を parse するのは Family の `validate_family_audit` だけ（§2.3）。github／handover／egress の `events.jsonl` を読む code は無く、`agentctl doctor` も audit を見ない。`StateLayout` に `github_broker_audit_file` は無く、path は `BrokerSession.create` が文字列で組む（`github_broker.py` L82、L105）。
- 実 host smoke 手順書は key と `jq` の allowlist を固定する。`docs/phase3-github-broker-smoke-test.md` L136（path）、L159（観測 key 集合 `bytes, operation, policy_version, project, repository, run, status, timestamp`）、L165（`status=ok` で `stage` 無し）。`docs/phase4-stabilization-smoke-test.md` L251-252（`jq -c '{timestamp,operation,status,stage}'`）、L277-285。`docs/phase2-smoke-test.md` L147、L166-169（handover の `authentication`／`content-policy` stage）。`docs/family-issue-create-broker-smoke-test.md` L111、L133（固定 6 field、9 event）。`docs/phase3-github-broker.md` L187、L207、L220-224、`docs/phase2-claude-code.md` L191、`docs/family-issue-create-broker.md` L105 も schema を記述し、`tests/container/test_docs.py`（L598、L623、L649-652、L765、L838、L1109-1154、L1341）がそれらの文を固定する。
- Phase 6 の完了条件は「既存の実 host smoke 手順が**変更なしで** PASS」。broker 固有 key を `details` へ寄せる envelope や timestamp 形式の統一は、上記手順書の key 集合記述と Family validator に直接当たる。

golden（意図的更新が必要になる fixture）: `test_broker_frame_golden.py`（`b1198d1`）、`test_broker_audit_golden.py`（`4c555fd`）、`test_broker_egress_golden.py`（`0ca61c2`）、`tests/fixtures/broker_github_golden.json`（`a69bb78`、support 側に基準 hash の記録なし）、`tests/fixtures/broker_family_golden.json`（`39fbc5e`、JSON 内に `baseline` あり）。

## 6. `StateLayout`・Mount 型・`PROTOCOL_VERSION`

- `StateLayout`（`state.py` L184-234）の broker 3 点は `root/<name>-broker`、`.../r/<sha256(project_id)[:12]>`、`.../audit/events.jsonl` で、3 つの `*_project_label` は同一関数。Family は別 layout（`family_state.py` L25-69）で audit が **project 毎**（`family/projects/<id>/audit/events.jsonl`）。名前を保てば `broker_root(name)` へ畳んでも on-disk path は変わらないが、github／handover の `BrokerSession.create` は layout を通さず path を再構成し、`podman.py` L234-247 も family path を inline で再導出する。broker root を検査する doctor／migration code は無い（`migration.py` は Claude config のみ）。literal path を固定する test: `test_state.py` L60-91、`test_family_state.py` L40-46、`test_podman.py` L457 ほか。
- Mount 型: `BrokerRuntimeMount(run_dir, repository)`、`HandoverRuntimeMount(run_dir)`、`EgressRuntimeMount(run_dir, project_id, agent)` は無状態 frozen dataclass で run dir を read-only bind し capability を file で渡す。`FamilyRuntimeMount` だけが socket **file** を rw bind し capability を env で渡し、fd を保持して `revalidate()` する。`podman.py` の mount／env 文字列は `test_podman.py` L895-920 ほかと `tests/integration/test_egress_podman.py` L315-318 が固定する。
- `PROTOCOL_VERSION = 1` は 4 protocol module に定義、host／container 双方で検査。bump は 4 定数、egress の literal 2 か所、`handover_broker_client._self_check`（`== 1` を hard assert、doctor が使用）、全 wire golden、container image の同時 roll を要する。wire 形式を変えない限り bump に利点は無い。

## 7. CI と検証経路

- required CI（`.github/workflows/ci.yml`）: `bin/lint` → `tests/codex` → `tests/container` → socket integration 3 本（github／handover／egress、`ResourceWarning` で fail）→ `git diff --check`。`podman-integration` は 14 test 固定で skip 不可。
- `tests/integration/test_family_intake_socket.py`（実 socket、11 件）と `test_family_forced_unknown.py` は CI に含まれず、`docs/family-issue-create-broker-smoke-test.md` L22-23、L98 の手順でのみ実行する。stage 2 で Family lifecycle を触る場合はこの 2 本の local 実行が必須。
- local Podman は本環境で `not run — podman unavailable`。実 host smoke は stage 1（6-6）から未実施。

## 8. stage 2 で決めるべき事項（未決、推奨は別途提示）

1. **readiness／peer 検証**: Family の request 毎 pid 祖先検証を kernel の接続毎 peer policy として一般化するか、accept 前 gate へ変えるか。他 3 broker へ祖先検証を適用するか（保証強化＝拒否 timing の変更）。
2. **fail-closed lifecycle**: `deactivate()` 失敗時の継続・報告・再試行契約（#98）。cleanup の順序（仕様 L150 と実装の食い違い）、inode 同一性検査、失敗後の再試行可否、Family の descriptor 保持 cleanup を kernel へ入れる形。
3. **audit**: 共通 key を kernel で必須化するか、`details` へ寄せるか、timestamp 形式を統一するか。Family validator と smoke 手順書の固定事項（§5）との両立。
4. **GitHub 完全統一**: 表（§3）の各行を変更項目として承認するか。CLI stderr の文言変化を許容するか。
5. **handover transport**: kernel frame error に種別（size／schema）の口を設けて transport を乗せるか。
6. **capability reader**: github／egress を kernel reader に統一するか。egress の 0400 と timeout 無しをどうするか。
7. **`PROTOCOL_VERSION`、`StateLayout`、Mount 型**: 変更するか、stage 2 の範囲外と明記するか。
8. **順序**: 6-6 実 host smoke（stage 1 gate）を stage 2 の code 変更前に実施するか。stage 2 は audit・lifecycle・cleanup という 6-6 が証明する対象そのものを変えるため、先に stage 1 の証拠を固定しないと両 stage の切り分けができない。
