# serial-flash-relay

ESP32 / ESP8266 / ESP8285 と RP2040 / RP2350（Raspberry Pi Pico 系）のための
**シリアルリレー + 書き込みサーバ**。書き込み機能を持たない**汎用シリアル
リレー**も同梱しています。書き込み用のリレーは 1 プロセスで次の 2 つを提供します。

- **シリアルリレー**: USB シリアル（`/dev/ttyUSB*` / `/dev/ttyACM*`）を
  TCP（既定は `127.0.0.1`）に中継する。コンソール監視や esptool からの利用に。
- **HTTP 書き込み・リセット API**: ホスト側で esptool / picotool を実行して
  書き込む。DTR/RTS または picotool 経由のリセットも HTTP から実行できる。
  サンドボックスや別マシンから `curl` 1 発で操作できる。

```text
  ┌────────────────────┐  TCP 4000+            ┌──────────────────────┐   USB   ┌────────────┐
  │ sandbox / 別マシン  │ ───────────────────► │ esp_relay.py         │ ◄─────► │ ESP32      │
  │ curl / console     │                      │ rp_relay.py          │         │ ESP8266/85 │
  └────────────────────┘  HTTP 8000+           │ serial_relay.py      │         │ RP2040     │
                         ───────────────────► │ (host process)       │         │ RP2350     │
                                              │ esptool / picotool   │         └────────────┘
                                              └──────────────────────┘
```

USBIP / vhci 経由で `/dev` が直接見えない環境や、TCP→pty 経由の書き込みが
不安定な環境でも、**ホスト側の 1 プロセスがポートを所有**して確実に書き込める
ようにするのが目的です。Python 標準ライブラリだけで動きます（外部依存は
esptool / picotool のみ）。

## ファイル構成

```
serial-flash-relay/
├── esp_relay.py      # ESP32 / ESP8266 / ESP8285 用リレー + esptool 書き込み API
├── rp_relay.py       # RP2040 / RP2350 用リレー + picotool / BOOTSEL 書き込み API
├── serial_relay.py   # 書き込みなしの汎用シリアル <-> TCP リレー
├── README.md
└── .gitignore
```

リレーはそれぞれ**単体で完結**しています（共通モジュールなし）。コピーして
必要なものだけ使うこともできます。

## 必要環境

- Linux（`termios` / `fcntl` / `/sys` を使用。Windows / macOS 非対応）
- Python 3.9 以上（3.14 で動作確認）
- シリアルポートへの権限（`dialout` グループ。反映には再ログインが必要）

```bash
groups | grep dialout || sudo usermod -aG dialout "$USER"
```

- **ESP 系**: esptool が必要。次の順で自動検出します。
  1. `--esptool PATH`
  2. 環境変数 `ESPTOOL`
  3. `PATH` 上の `esptool` / `esptool.py`
  4. `~/.local/bin/esptool`
  5. Arduino コア内 `~/.arduino15/packages/*/tools/esptool_py/*/esptool`
     （`ARDUINO_DATA_DIR` で変更可）

  esptool **v4（`write_flash` / `default_reset`）と v5（`write-flash` /
  `default-reset`）の両方**に対応。バージョンは `esptool version` から判定し、
  判定できない場合は v5 構文で試して引数エラーなら v4 構文で自動再試行します。
- **RP2040 / RP2350**: picotool は**任意**。見つからなければ BOOTSEL マスストレージ
  にフォールバックします。picotool の検出順は `--picotool` → `PICOTOOL` 環境変数
  → `PATH` → `~/.local/bin/picotool` → Arduino コア内 `pqt-picotool`。

## クイックスタート

### ESP32 / ESP8266 / ESP8285

```bash
# ホスト側（常駐）
python3 esp_relay.py --port /dev/ttyUSB0
#   既定: flash API http://127.0.0.1:8000 / console tcp://127.0.0.1:4000
#   8000/4000 が使用中なら 8001/4001 などに自動で移る（起動ログを確認）

# 以下は既定の 8000 を使う例。起動ログの "flash API on ..." のポートに読み替え
# 状態確認
curl -sS http://127.0.0.1:8000/health

# 書き込み（例: ESP32 の 4 イメージ）
curl -sS -X POST http://127.0.0.1:8000/flash \
  -H 'Content-Type: application/json' \
  -d '{
    "images": [
      {"address": "0x1000",  "file": "/abs/path/bootloader.bin"},
      {"address": "0x8000",  "file": "/abs/path/partitions.bin"},
      {"address": "0xe000",  "file": "/abs/path/boot_app0.bin"},
      {"address": "0x10000", "file": "/abs/path/app.bin"}
    ]
  }'

# 単一イメージなら merged を使う
curl -sS -X POST http://127.0.0.1:8000/flash \
  -H 'Content-Type: application/json' \
  -d '{"merged": "/abs/path/merged.bin"}'

# リセット（アプリ起動）
curl -sS -X POST http://127.0.0.1:8000/reset \
  -H 'Content-Type: application/json' \
  -d '{"mode": "run"}'
```

- チップは既定で **auto**（esptool が自動判別）。ESP8266 を明示するときは
  `"chip": "esp8266"`、ESP8285 は `"chip": "esp8285"`（内部で `esp8266` に変換）。
- 書き込み中は**接続中のコンソールセッションを切断してポートを明け渡します**
  （`--flash-wait` 秒待っても解放されなければ 409）。書き込み後に再接続してください。
- コンソールを見る最小クライアント:

```python
import socket
s = socket.create_connection(("127.0.0.1", 4000), timeout=5)   # 実際の TCP ポート
s.sendall(b"ESPRELAY:console\n")       # リセットしてアプリ起動ログを見る
while True:
    data = s.recv(4096)
    if not data:
        break
    print(data.decode("utf-8", "replace"), end="")
```

> **ESP32 のブートローダー位置に注意**
> 使用する Arduino コアによってはブートローダーは **`0x1000`** です（`0x0`
> ではない）。コアの `build.bootloader_addr` に合わせてください。`0x0` に
> 書くと書き込み自体は成功しても `invalid header: 0x...` のブートループに
> なります。Arduino IDE の「コンパイル後のコマンドを表示」で実際の
> アドレスを確認するのが確実です。
>
> ESP8266 / ESP8285 のアドレス構成は ESP32 と異なります。ボードのフラッシュ
> レイアウト（Arduino コアが表示する esptool コマンド）に従ってください。

DTR/RTS による自動リセットが使えない配線（RP2040 ブリッジ越しの ESP8285 など）
では `--reset none` を付けてリレーを起動し、書き込み時は
`"before": "no-reset", "after": "no-reset"` を指定します。

```bash
python3 esp_relay.py --port /dev/ttyUSB1 --reset none --chip esp8266
```

### RP2040 / RP2350（Pico / Pico W / Pico 2 / RP2040+ESP8285 クローン）

```bash
# ポートを自動検出（VID 2e8a。複数ボードがある場合は --usb-serial を指定）
python3 rp_relay.py
#   既定: flash API http://127.0.0.1:8000 / console tcp://127.0.0.1:4000
#   8000/4000 が使用中なら 8001/4001 などに自動で移る（起動ログを確認）

# ポートと USB シリアルを固定する場合
python3 rp_relay.py --port /dev/ttyACM1 --usb-serial 45474E621A04FA4A

# 状態確認（起動ログの HTTP ポートに読み替え）
curl -sS http://127.0.0.1:8000/health

# 書き込み（ゼロタッチ: picotool が reset interface 経由で BOOTSEL へ）
curl -sS -X POST http://127.0.0.1:8000/flash \
  -H 'Content-Type: application/json' \
  -d '{"file": "/abs/path/firmware.uf2"}'

# リセット（アプリ起動 / BOOTSEL）
curl -sS -X POST http://127.0.0.1:8000/reset \
  -H 'Content-Type: application/json' \
  -d '{"mode": "run"}'
curl -sS -X POST http://127.0.0.1:8000/reset \
  -H 'Content-Type: application/json' \
  -d '{"mode": "bootsel"}'
```

`file` は **ホスト側で読めるパス**です（アップロードはしません）。

RP2040 と RP2350 は USB の VID がいずれも `2e8a` で、CDC の `ttyACM*` も
共通なので、**シリアル追跡と picotool 書き込みは同じコードで動きます**。
サードパーティ製 RP2350 ボードなどで VID が異なる場合は `--vid` を指定して
ください。

#### ゼロタッチ書き込みの条件

picotool が自動で BOOTSEL にリセットして書き込むには、実行中のファームウェアが
**picotool reset interface** を公開している必要があります。Arduino の
`arduino:rp2040` コアなら、`uploadmethod=picotool` でビルドしたスケッチが
該当します（`-DENABLE_PICOTOOL_USB`）。picotool が認識する USB PID は
ファミリごとに異なります。

| ファミリ | ボード例 / FQBN | USB PID |
|---|---|---|
| RP2040 | `rpipico` / `rpipicow`（Pico W） | `0x000a` |
| RP2350 | `rpipico2`（Pico 2）/ 各社 RP2350 ボード | `0x000f` |

```bash
# RP2040 Pico W: ボード既定の PID は 0xf00a なので 0x000a に上書きする
arduino-cli compile \
  --fqbn rp2040:rp2040:rpipicow:uploadmethod=picotool \
  --build-property "build.usbpid=-DUSBD_PID=0x000a" \
  <sketch>

# RP2350 Pico 2: ボード既定の PID が 0x000f なのでそのままでよい
arduino-cli compile \
  --fqbn rp2040:rp2040:rpipico2:uploadmethod=picotool \
  <sketch>
```

- RP2040 の素の `rpipico` は既定 PID が `0x000a` です。Pico W だけ上書きが
  必要です（`0xf00a` のままでは picotool から見えません）。
- RP2350 の Pico 2 は既定 PID が `0x000f` です。他の RP2350 ボードで
  独自 PID を使っている場合は `-DUSBD_PID=0x000f` を明示してください。
- RP2040 + ESP8285 の WiFi なしボード（RP-ESP）は `rp2040:rp2040:rpipico` を
  使います。
- **CircuitPython は reset interface を出さない**ためゼロタッチ不可です。
  最初の 1 回だけ BOOTSEL を押しながら USB を挿し、
  `{"file": "....uf2", "method": "storage"}` で書き込んでください。以降は
  上記条件の Arduino スケッチならゼロタッチになります。
- BOOTSEL ドライブのボリュームラベルは RP2040 が `RPI-RP2`、RP2350 が
  `RP2350` です。リレーはラベルに依存せず、**`INFO_UF2.TXT` がある
  マウントを自動検出**します（Arduino の UF2 アップローダと同じ方式）。

> picotool は 2.x（Rust 版）を推奨。古い 2.3.0 が USBIP/vhci 経路で
> セグフォした報告があるため、2.3.1 以降を使ってください。RP2350 の
> サポートは picotool 2.x 以降です。

### 汎用シリアルリレー（書き込みなし）

書き込みもリセットも不要で、**任意のシリアルデバイス**をそのまま TCP に
流したい場合は `serial_relay.py` を使います。HTTP API はありません。

```bash
python3 serial_relay.py --port /dev/ttyUSB2 --baud 115200
# 既定 TCP 4000。使用中なら 4001, 4002, ... に自動で移る（起動ログを確認）
```

- 受信したバイトを**そのまま**双方向に転送します（`ESPRELAY:` プレフィックス
  なし、DTR/RTS 操作なし）。
- クライアントは同時に 1 つ。接続中に別のクライアントが来た場合は、最初の
  クライアントが切れるまでバックログで待ちます。
- ポートは起動時に開き、クライアントが切れても開いたままにします。接続時に
  inode identity を確認し、同じ名前で別デバイスに入れ替わっていたら開き直します。
- セッション中にデバイスが消えた場合はクライアントを切断します（プロセスは
  生存し、次のクライアントが再接続を試みます）。USB 再列挙をまたいで
  セッションを維持したい場合は `rp_relay.py` を使ってください。
- 起動時にデバイスが無い場合は `--wait 30` のように待ち時間を指定できます。

```python
import socket
s = socket.create_connection(("127.0.0.1", 4000), timeout=5)   # 起動ログのポート
s.sendall(b"AT\r\n")
print(s.recv(4096))
```

ESP のリセット付きコンソールは `esp_relay.py`、RP の再列挙追跡は
`rp_relay.py` を使ってください。

## ポートの自動選択と名前

### ポート

既定値は次のとおりです。

| スクリプト | HTTP | TCP |
|---|---|---|
| `esp_relay.py` | 8000 | 4000 |
| `rp_relay.py` | 8000 | 4000 |
| `serial_relay.py` | — | 4000 |

使用中なら番号を +1 して空きを探します。`esp_relay.py` / `rp_relay.py` は
**HTTP と TCP をペアで +1** するため、8000 だけが使用中の場合は
**8001 と 4001** になります（8001 と 4000 にはなりません）。

```text
[esp-relay] requested port pair HTTP 8000 / TCP 4000 is unavailable — using 8001 / 4001
[esp-relay] flash API on http://127.0.0.1:8001 (...)
[esp-relay] serial relay on tcp://127.0.0.1:4001 (...)
```

- `--http` / `--tcp` は**開始ポート**です。自動選択はそこから +1 します
  （例: `--http 9000 --tcp 5000` で 9000 が使用中なら 9001/5001）。
- 完全に固定したい場合は `--no-auto-port` を付けます。開始ポートが使用中なら
  エラーで終了します。
- 実際に選ばれた値は起動ログと `/health` の `http_port` / `tcp_port` で
  確認できます。
- 複数のリレーを同時に起動すると、起動順に空きペアが割り当てられます。
  ESP と RP を両方動かす場合は、それぞれの起動ログのポートを使ってください。

### 名前

`--name NAME` で、ログの `[esp-relay]` の部分と `/health` の `name` を
変更できます。既定は `esp-relay` / `rp-relay` / `serial-relay` です。

```bash
python3 esp_relay.py --port /dev/ttyUSB0 --name esp-lab
# [esp-lab] flash API on http://127.0.0.1:8000 (...)
curl -sS http://127.0.0.1:8000/health
# -> {"name":"esp-lab", ...}
```

名前は 64 文字以内で、改行などの制御文字は使えません。`serial_relay.py` は
HTTP API を持たないため、ログの接頭辞だけに使われます。

## HTTP API

ESP / RP リレーの HTTP API（書き込みとリセット）です。`serial_relay.py` には
HTTP API はありません（TCP リレーのみ）。以下の例の `8000` は既定値です。
起動ログの `flash API on ...` に出た HTTP ポートに読み替えてください。

### `GET /health`

ESP / RP リレー共通の基本フィールド:

| フィールド | 意味 |
|---|---|
| `ok` | 常に `true` |
| `service` / `role` / `version` | サービス名 / `esp` or `rp` / バージョン |
| `name` | `--name` で指定した名前 |
| `http_port` / `tcp_port` | 実際に選ばれた HTTP / TCP ポート |
| `port` | 現在のシリアルデバイス（未接続なら `null`） |
| `port_ready` | リレーが fd を保持しているか |
| `baud` / `rescan` | 設定値 |
| `serial_active` / `flash_busy` | コンソール接続中 / 書き込み中 |
| `uptime_s` | 起動からの秒数 |

ESP 固有: `esptool`, `esptool_version`, `default_chip`, `flash_wait_s`。
RP 固有: `usb_serial`, `vid`, `picotool`, `picotool_kind`
（`rust` / `python`）, `flash_method`。

### `POST /flash`（ESP）

| フィールド | 既定 | 説明 |
|---|---|---|
| `images` | — | `[{"address": "0x1000", "file": "/abs/a.bin"}, ...]` |
| `merged` | — | 単一イメージ。`images` とは**排他** |
| `port` | `--port` | esptool に渡すポート。通常は省略 |
| `chip` | `auto` | `auto` / `esp32` / `esp8266` / `esp8285`（= `esp8266`）など |
| `baud` | `115200` | 9600〜2000000 |
| `before` | `default-reset` | `default-reset` / `usb-reset` / `no-reset` / `no-reset-no-sync` |
| `after` | `hard-reset` | `hard-reset` / `soft-reset` / `no-reset` / `no-reset-stub` / `watchdog-reset`※ |
| `no_stub` | `false` | `--no-stub`（ROM ローダー） |
| `no_compression` | `false` | 圧縮を無効化 |
| `erase_all` | `false` | 書き込み前に全消去 |
| `timeout_s` | `1800` | 10〜3600 秒 |
| `dry_run` | `false` | 実行せず組み立てた `cmd` だけ返す |

※ `watchdog-reset` は esptool v5 のみ。v4 では 400 を返します。
`before` / `after` はハイフン・アンダースコアどちらでも受け付けます
（v4/v5 の表記差は内部で吸収）。

成功レスポンス:

```json
{
  "ok": true,
  "exit_code": 0,
  "elapsed_s": 12.3,
  "chip": "auto",
  "port": "/dev/ttyUSB0",
  "cmd": ["/usr/bin/esptool", "--port", "...", "write_flash", "..."],
  "log": "esptool.py v4.7.0 ..."
}
```

`dry_run` のときは `{"ok": true, "dry_run": true, "cmd": [...]}`。

### `POST /reset`（ESP）

DTR/RTS を使って基板をリセットします。TCP 接続時のリセットと同じシーケンスです。

| フィールド | 既定 | 説明 |
|---|---|---|
| `mode` | `run` | `run`（アプリ起動）/ `download`（ROM ダウンロードモード） |

```bash
curl -sS -X POST http://127.0.0.1:8000/reset \
  -H 'Content-Type: application/json' -d '{"mode":"run"}'
# -> {"ok":true,"mode":"run","port":"/dev/ttyUSB0",
#     "message":"reset sequence sent (run)"}
```

- コンソール接続中でも実行できます。セッションは切断されず、リセット後の
  起動ログがそのまま流れます。
- 書き込み中は 409。DTR/RTS を制御できないアダプタでは 500。
- `--no-serial` で起動している場合は 503 です。
- `download` は ROM ダウンロードモードに入ったままになります。アプリに戻すには
  `run` を送るか、書き込みを行ってください。

### `POST /flash`（RP）

| フィールド | 既定 | 説明 |
|---|---|---|
| `file` | — | UF2（または picotool が扱える .bin/.elf）の**ホストパス** |
| `method` | `--flash-method`（`auto`） | `auto` / `picotool` / `storage` |

- `auto`: picotool を試し、失敗したら BOOTSEL マスストレージ。
- `picotool`: ゼロタッチ。reset interface がなければエラー（ヒント付き）。
- `storage`: RPI-RP2 ドライブへコピー。**UF2 のみ**。BOOTSEL が必要で、
  自動マウントされていなければ `sudo -n mount` を使います。

成功レスポンス:

```json
{
  "ok": true,
  "exit_code": 0,
  "elapsed_s": 8.1,
  "method": "picotool",
  "tool": "/home/user/.local/bin/picotool",
  "log": "picotool v2.3.1 ..."
}
```

### `POST /reset`（RP）

picotool の reset interface 経由で RP2040 / RP2350 を再起動します。

| フィールド | 既定 | 説明 |
|---|---|---|
| `mode` | `run` | `run`（アプリ起動）/ `bootsel`（BOOTSEL モードへ） |

```bash
curl -sS -X POST http://127.0.0.1:8000/reset \
  -H 'Content-Type: application/json' -d '{"mode":"run"}'
# -> {"ok":true,"mode":"run","elapsed_s":1.2,"tool":"...","log":"..."}
```

- `mode=bootsel` は BOOTSEL に入れて RPI-RP2 ドライブを出します。storage 書き込みの
  前準備に使えます。
- シリアルポートは不要です（`--no-serial` でも使用可）。コンソール接続中でも
  実行でき、reboot 後は auto-rescan で TCP セッションが継続します。
- 実行中のファームウェアが picotool reset interface を持たない場合
  （CircuitPython など）は 500。電源再投入または BOOTSEL 接続を使ってください。
- 書き込み中は 409。

### ステータスコード

| コード | 意味 |
|---|---|
| 200 | 成功（`dry_run`・`/reset` 含む） |
| 400 | JSON / フィールド / ファイルパス / アドレス / `mode` の不正 |
| 404 | 不明なパス |
| 409 | 書き込み・リセットが既に実行中（ESP はコンソールがポートを解放しない場合も） |
| 500 | 書き込み / リセットツールの失敗、DTR/RTS 非対応（ログに詳細） |
| 503 | ESP の `/reset` でシリアルポートが使えない / `--no-serial` |

タイムアウトは `500` + `exit_code: 124`（ESP の書き込み）またはログ内
`TIMEOUT` で返ります。

## TCP プロトコル

接続直後に次の 1 行を送ると、モードを指定できます（任意）。

```
ESPRELAY:<mode>\n
```

| mode（ESP） | 動作 |
|---|---|
| `download` / `flash` / `boot` | DTR/RTS で ROM ダウンロードモードへ |
| `run` / `console` / `app` | DTR/RTS でアプリを再起動 |
| `none` / `skip` / `no-reset` | リセットせずそのままブリッジ |
| 未知の値 / プレフィックスなし | `download`（従来互換） |

RP ではモードは**情報ログのみ**（ESP のような DTR/RTS ブートストラップが
ないため）。同じクライアント実装を使い回せるよう、プレフィックスは吸収して
UART には流しません。ESP / RP のリレーはどちらも、プレフィックスが分割
パケットで届いても 1 行になるまでバッファします（`ESPRELAY:con` + `sole\n`
のような分割でも UART に漏れません）。

その後は双方向の透過ブリッジです。TCP 側から DTR/RTS は操作できないため、
ESP のリセットは**接続時のリレー側シーケンス**で行います（HTTP の
`POST /reset` からも同じシーケンスを実行できます）。esptool を TCP
経由で使う場合は、このリセット前提になります（ホスト側の HTTP API を使う方が
確実です）。`serial_relay.py` はこのプレフィックスプロトコルを使わず、
接続直後から全バイトを透過転送します（リセットもなし）。

## RP シリーズ: 再列挙追跡と、修正した既知の問題

Pico / Pico 2 は書き込み・リセット・抜き差しのたびに USB を再列挙します。
カーネルは**同じ名前（`ttyACM8` → `ttyACM8`）を新しいデバイスに再利用**する
ことがあり、名前比較では古い fd をいつまでも使い続けてしまいます。
USBIP/vhci では死んだ tty fd が `select()` で永久にブロックし、EOF も EIO も
返らないこともあります。`rp_relay.py` はこの問題群を次のように解消しています。

| 問題（旧実装） | 修正 |
|---|---|
| tty を**名前**で追跡 → 同名再利用を見逃す | `(st_dev, st_ino)` の**inode identity** で追跡 |
| クライアント未接続中の再列挙で死んだ fd が残る | **接続時にも identity を検証**して開き直す |
| ゾンビ fd が EOF を返さず `select()` が固まる | セッション中も **5 秒ごとに identity をポーリング** |
| デバイス消失時、**180 秒ブロッキング**で再スキャン → ブリッジ停止・クライアント切断も検知不能 | **select ループ内の非ブロッキング再スキャン**に変更。クライアントは接続したまま待機でき、切断も即検知 |
| 再スキャン / 接続時 reswap で **fd リーク**（古い fd・dup を閉じない） | 置換時に必ず旧 fd を close。dup 失敗時も二重 close しない |
| 複数ボードが同じ VID → **先頭を誤選択** | USB シリアルで照合し、**推測しない**（`--usb-serial`） |
| `sys.exit()` が `open_serial()` 内 → 再スキャン中にスレッド/プロセスが落ちる | `SerialError` 例外に統一。失敗してもプロセスは生存 |
| プレフィックスの分割受信で **UART に漏れる** | 1 行バッファで完全に吸収 |
| 外部 `tools/flash_pico.sh` 依存（リポジトリ外に置くと動かない） | picotool / BOOTSEL 書き込みを**Python 内に内蔵** |
| `--port` 必須 | **VID 自動検出**（`--port` 省略可）。HTTP API はデバイス待ちの間も利用可 |
| 固定 115200 を cflag に埋め込み / HUPCL で close 時リセット | ボーレートを正しく設定し、**HUPCL を解除** |
| ポート消失時にクライアントへ何も言わず終了 | 既定 180 秒（`--rescan-timeout`、0 で無限）待って再接続。待機中はデータを捨てたことをログ |

> **USBIP のゾンビセッションだけはリレーでは治せません。** デバイスが再列挙
> すらせず、USBIP のセッション自体が死んでいる場合は、OpenWrt 側で USB を
> 抜き差しするか `usbip detach/attach` でセッションを作り直してください。
> リレーは「デバイスが戻ってきたら自動で追従する」ところまでを担当します。

`--no-rescan` を付けると追跡を無効化します。ポート名が変わる環境で名前を固定
したい場合などに使います。

## 修正した潜在バグ（ESP / 共通）

| 症状 | 原因 | 修正 |
|---|---|---|
| esptool v4 で書き込みが即失敗 | `write-flash` / `default-reset` は v5 構文 | v4/v5 をバージョン判定して使い分け。未知版は自動再試行 |
| ESP8266 / ESP8285 が既定で失敗 | 既定チップが `esp32` 固定 | 既定 `auto`、`esp8285` は `esp8266` に変換 |
| `merged` + `images` を同時指定すると**先頭に黙って上書き** | 両方を連結していた | 400 で排他エラー |
| `baud` が文字列 / `images` が非 dict で 500 + トレースバック | 検証なし | すべて 400 で返す |
| 書き込みが**最大 300 秒待たされる**（コンソール接続中） | セッション終了をロック待ち | コンソールを**プリエンプト**して切断。`--flash-wait` で上限 |
| シリアルポートが無いと起動できない | 先にシリアルを open していた | HTTP API を先に起動し、デバイスは後から待つ |
| ポート消失後に再接続不能 | 再オープン処理なし | identity チェック + 同一パスの再オープン |
| プレフィックスが UART に漏れる | 1 回の `recv` で判定 | 1 行バッファ |
| クライアントが読まないと CPU を回し続ける | `send` の EAGAIN を無限ループ | 書き込み可能待ち + タイムアウト |
| `EAGAIN` をデバイス消失と誤判定 | `os.read` の例外をすべて EOF 扱い | `BlockingIOError` / `InterruptedError` は継続 |
| SIGTERM でコンソール接続中に終了しない | ブリッジが停止フラグを見ない | 0.5 秒以内にループ先頭で検知 |
| close 時に DTR が落ちてボードがリセット | HUPCL 有効 | HUPCL を解除 |

## 既知の制限・注意

- **Linux 専用**です（`termios` と `/sys` を使用）。
- **認証はありません。** 既定で `127.0.0.1` にのみ bind します。
  `--bind 0.0.0.0` にすると、ホスト上の任意ファイルを書き込む API を
  ネットワークに公開することになります。必要なら SSH ポートフォワードや
  リバースプロキシ + 認証を使ってください。
- `file` は**ホスト側のパス**です。リレーはファイルを受け取りません。
- ESP の書き込みは**ポートを排他**します。接続中のコンソールは切断されます。
  書き込み後に再接続してください。
- ESP の再接続追跡は**同じデバイスパス**のみです（VID スキャンはしません）。
  `ttyUSB0` → `ttyUSB1` のように変わる場合は `--port` を変えて再起動するか、
  udev の固定リンクを使ってください。
- TCP ブリッジ経由では DTR/RTS を操作できません。ESP ではリレーが接続時に
  1 回だけリセットします。リセット不要な配線では `--reset none`。
- HTTP の `/reset`（ESP）は DTR/RTS 制御が必要です。DTR/RTS が配線されていない
  アダプタ（RP2040 ブリッジ越しの ESP8285 など）では 500 になります。
- HTTP の `/reset`（RP）は picotool の reset interface が必要です。
  CircuitPython など対応していない FW では使えません（電源再投入 / BOOTSEL）。
- RP2040 / RP2350 の `storage` は BOOTSEL 状態が必要です。自動マウントされて
  いない環境では `sudo -n mount` を試みます（パスワード入力はできません）。
  書き込み先が本当に BOOTSEL ドライブ（`RPI-RP2` / `RP2350`）か
  `INFO_UF2.TXT` で確認してからコピーします。
- `serial_relay.py` は単一クライアント・リセットなしです。デバイス消失時は
  クライアントを切断し、次の接続で開き直します。USB 再列挙をまたいで
  セッションを維持する用途は `rp_relay.py` を使ってください。
- クライアントが長時間データを読まない場合、TCP 送信は 30 秒でタイムアウト
  してセッションを閉じます（UART 側のバッファ詰まりを避けるため）。
- リレーが tty を開いたままなので、同じポートを他のコンソールツールで同時に
  開かないでください。picotool は USB 経由なので併用できますが、esptool は
  同じ tty を使うため HTTP API 経由で実行してください。

## トラブルシューティング

| 症状 | 確認・対処 |
|---|---|
| `permission denied` | `groups` に `dialout` があるか。追加後は再ログイン |
| `esptool not found` | `esptool version`、`--esptool`、`ESPTOOL` を確認 |
| `port busy` / 409 | `serial_active` を確認。コンソールを閉じるか `--flash-wait` を延ばす |
| 書き込み成功後に起動しない（ESP32） | ブートローダーを `0x1000` に書いているか。`invalid header` は位置違いの典型 |
| `invalid header: 0x...` | 同上。`0x0` ではなくコアの `build.bootloader_addr` に合わせる |
| picotool が `unable to connect` | USB 権限。下記 udev ルールを追加 |
| `Unable to locate reset interface` | CircuitPython などゼロタッチ非対応。BOOTSEL + `method=storage` |
| `ttyACM` の番号が変わる | RP は自動追跡。複数ボードは `--usb-serial` |
| RP2350 が picotool から見えない | picotool 2.x、FQBN `rpipico2` + `uploadmethod=picotool`、USB PID `0x000f` を確認 |
| BOOTSEL ドライブが見つからない | `RPI-RP2` / `RP2350` の自動マウントか `lsblk` を確認。リレーは `INFO_UF2.TXT` で判定 |
| USBIP で転送が完全に死んでいる | サーバ側で USB 抜き差し / `usbip detach` → `attach` |
| DTR/RTS リセットが効かない | `--reset none` にして手動リセット、または配線を確認 |
| `/reset` が 500（ESP） | DTR/RTS を制御できるアダプタか確認。無ければ手動リセット |
| `/reset` が 500（RP） | picotool reset interface 非対応。電源再投入 / BOOTSEL |
| `/reset` が 503（ESP） | `--no-serial` で起動している。シリアル有効で再起動 |

picotool 用 udev ルール（一度だけ）:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="2e8a", MODE="0660", GROUP="dialout"' \
  | sudo tee /etc/udev/rules.d/99-pico.rules
sudo udevadm control --reload && sudo udevadm trigger
```

## systemd の例

```ini
[Unit]
Description=serial-flash-relay (ESP32)
After=network.target

[Service]
User=YOUR_USER
Group=dialout
WorkingDirectory=/opt/serial-flash-relay
ExecStart=/usr/bin/python3 /opt/serial-flash-relay/esp_relay.py --port /dev/ttyUSB0 --name esp-lab
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

RP は `rp_relay.py`（`--port` 省略で VID 自動検出）に置き換えます。
汎用リレーは `serial_relay.py` です。HTTP API は localhost のみなので、
必要なら SSH トンネルで利用してください。
常駐させてポート番号を固定したい場合は `--no-auto-port` を付けるか、
起動ログと `/health` の `http_port` / `tcp_port` を確認してください。
リレーを使わず書き込み API だけを動かす場合は、ESP / RP のスクリプトに
`--no-serial` を付けられます（ESP は esptool が、RP は picotool が
ポートを直接開くため）。

## ライセンス

このリポジトリにはまだライセンスファイルがありません。公開前に MIT / Apache-2.0
などを選んで `LICENSE` を追加してください。
