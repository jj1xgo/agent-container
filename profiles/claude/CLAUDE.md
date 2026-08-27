# Managed Claude handover workflow

別 session または別 agent へ作業を引き継ぐ必要がある場合だけ handover を作成する。
作成前に Git status、直近 commit、実行済み test、未解決事項を確認し、推測した成功を書かない。

handover の作成には次の command だけを使い、完成した以下の 7 section を固定順序で各 1 回だけ stdin へ渡す。

`agent-handover create --title "引き継ぎタイトル"`

## 作業の目的

完了した内容を書く。

## 現在地

完了した内容を書く。

## 決定事項と理由

完了した内容を書く。

## 変更したファイル・commit・PR

完了した内容を書く。

## 検証結果

完了した内容を書く。

## 未解決事項とリスク

完了した内容を書く。

## 次の一手

完了した内容を書く。

credential、token、環境値、transcript 全文を含めない。broker が拒否した場合は停止し、sandbox や mount を弱めず、別 path への直接書き込みや fallback を行わない。
