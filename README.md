# LEAPER3CforMAC

**Native macOS USB Driver, Built-in Web UI (`leaper3c ui`), & CLI Programmer for LEAP Electronic LEAPER-3C (LP-3C FLASH WRITER)**

> Includes a **built-in browser Web UI (`leaper3c ui`)** — you can select ICs, check socket alignment diagrams, split `.nes` ROMs, and **program / blank-check / read / verify chips directly from your browser** on macOS without needing Windows or extra GUI software. ([日本語版はこちら / 日本語 README](#日本語))

[English](#english) | [日本語](#日本語)

---

<a id="english"></a>
## English

A user-space macOS USB driver (`leaper3c.py`), **built-in browser Web UI (`./leaper3c ui`)**, and CLI wrapper (`leaper3c`) for the **LEAP Electronic LEAPER-3C (LP-3C FLASH WRITER)** (`USB VID=0x04CE, PID=0x0A3C`), powered by `libusb` + `PyUSB`.

No Windows VM, no Boot Camp, and no macOS kernel extension (`.kext`) required — plug in USB + 12V power, run `leaper3c ui`, and **burn/read/verify EPROMs & Flash chips directly in your browser** from Apple Silicon (`arm64`) or Intel Macs (or use the CLI / FireMinipro desktop GUI).

### Features
- **Built-in Browser Web UI (`leaper3c ui`) — Ready to Burn Out of the Box**: Launches a local browser UI (`http://127.0.0.1:8765/`) where you can **program (burn), blank-check, read, verify, and erase chips** with live progress bars, Hex preview, `.bin` download, 28P/32P Slave socket SVG placement guide, `.nes` PRG/CHR auto-splitter, EN/JA switching, and 5 color themes (`Famicom`, `NES`, `Light`, `Gray`, `Dark`).
- **Zero Windows dependency**: Independent open-source user-space USB host implementation (`PyUSB` + `libusb`) for interoperability with LP-3C (`Firmware 1.30`) hardware. Contains no proprietary vendor software, drivers, or firmware binaries.
- **On-device IC database**: Queries firmware info (`1.30`) and the built-in 260-chip vendor/device list directly from the programmer (`info`, `list`, `chip-info`).
- **Full programmer workflow**: `blank-check`, `erase` (Flash/EEPROM), `read`, `verify`, and `program` with 512-byte block progress, CRC32 / SHA256 / SUM16 reporting, and `--dry-run`.
- **Hardware safety checks**:
  - Reads LP-3C `0x42` status register and aborts immediately on `OVER CURRENT FAIL` or `RUN FAIL`.
  - Prevents accidental `erase` on UV-EPROMs (`27C64`–`27C080`, which require a UV eraser).
- **Retro cartridge helper (`split-nes`)**: Extracts PRG / CHR binaries from iNES / NES 2.0 `.nes` ROM files and supports `--pad mirror` or `--pad ff` when burning smaller ROMs onto larger chips (e.g., 8 KB CHR onto a `27C256` / `27C512`, or 32 KB PRG onto an `SST39SF010A`).

### Hardware Notes & Warnings
1. **External Power Required**: Connect the **DC 12V / 500mA power adapter** (or batteries) and turn the unit power switch **ON**. USB bus power alone cannot generate programming Vpp voltages (~12.2V–12.9V).
2. **Socket Selection**: In PC mode, always insert the target IC into the **right-side ZIF socket (`Slave Textool`)**. The left-side `Master Textool` is only used as the copy source in stand-alone mode.
3. **IC Alignment (Bottom-Aligned / Lever Side)**: Align the notch / Pin 1 toward the **top (LCD side)**, and align the **bottom of the IC flush with the bottom edge (lever side)** of the `Slave Textool` as shown in the **IC package diagram** on the top panel. For **28-pin ICs** (such as `27C256`, `27C512`, `27C64`) in the 32-pin ZIF socket, leave the **top 2 pin rows (LCD side) empty**.
4. **Verification Scope**: Verified end-to-end on Apple Silicon macOS (`arm64`) with physical LP-3C Firmware `1.30` hardware: `info`, `list`, `chip-info`, `blank-check`, `read`, `verify`, `split-nes`, and **real hardware `program` + 100% read-back verification of a 32 KB `27C256` UV-EPROM (17.75 s)**. Electrical `erase` (for Flash/EEPROM) is verified via unit tests with mock USB; always verify chip type and alignment with `--dry-run` before programming.

### Tested Environment (Verified Hardware & Mac Specs)
- **Mac**: MacBook Air (`MacBookAir10,1`), Apple M1 (`arm64`, 8-core), 16 GB RAM
- **OS**: macOS 26.6.2 (Build `25G83`)
- **Runtime / Libraries**: `libusb 1.0.30` (Homebrew), `Python 3.11.3`, `pyusb 1.3.1`, `Qt 6.12.0` (with localized `FireMinipro`)
- **Programmer**: LEAP Electronic `LEAPER-3C` (`VID=0x04CE, PID=0x0A3C`, Firmware `1.30`)
- **Platform Note (Windows / Linux)**: The core USB driver (`leaper3c.py`) uses standard `PyUSB`/`libusb`, though current wrapper scripts (`leaper3c`, `minipro`, `.dylib` lookup) target macOS. Windows/Linux wrappers (`libusb-1.0.dll` / Zadig WinUSB on Windows 10/11 64-bit, or `udev` rules on Linux) have not yet been tested.

### Installation (macOS)

1. **Install prerequisites & clone repository**:
```sh
# Install libusb via Homebrew (Apple Silicon: /opt/homebrew/opt/libusb)
brew install libusb

# Clone this repository
git clone https://github.com/p0lyg0n/LEAPER3CforMAC.git
cd LEAPER3CforMAC

# Create a Python virtual environment and install PyUSB
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

2. **Install global CLI commands (`leaper3c`, `minipro`) & optional Applications launcher / FireMinipro bridge**:
```sh
# Links /opt/homebrew/bin/leaper3c and /opt/homebrew/bin/minipro
# If /Applications/fireminipro.app is installed, it also patches the bridge automatically
./install_fireminipro_bridge.sh

# (Optional) Install "/Applications/LEAPER-3C Web UI.app" for 1-click launch from Finder/Spotlight
./install_webui_app.sh
```

3. **Verify hardware connection**:
Connect the **DC 12V AC adapter** to your LEAPER-3C, turn the power switch **ON**, connect USB to your Mac, and run:
```sh
leaper3c info
# Or if you skipped install_fireminipro_bridge.sh:
./leaper3c info
```

4. **(Optional) Install [FireMinipro](https://github.com/Jartza/fireminipro) Desktop GUI**:
   - Place `fireminipro.app` into `/Applications/` (from upstream releases or built from the fork branch [`p0lyg0n/fireminipro` `feat/themes`](https://github.com/p0lyg0n/fireminipro/tree/feat/themes) with Japanese UI, 5 themes, and a dedicated Settings window).
   - Re-run `./install_fireminipro_bridge.sh` so `fireminipro.app` uses the `LEAPER-3C` `minipro` bridge.

---

### How to Use

#### Option A: Built-in Local Web UI (`leaper3c ui` / Applications App) — Recommended
1. Connect 12V power + USB and double-click **`LEAPER-3C Web UI`** in `/Applications` (installed via `./install_webui_app.sh`), or start it from the terminal:
   ```sh
   leaper3c ui
   ```
   Your default browser opens `http://127.0.0.1:8765/` automatically.
2. Confirm the top-right badge shows **Connected (FW 1.30)** (`接続済み`). You can switch **Language** (`日本語` / `English`) and **Theme** (`Famicom`, `NES`, `Light`, `Gray`, `Dark`) from the header.
3. Search and choose your **Target IC** (e.g., `27C256 @ 27XXX`), then insert the chip into the **right-side `SLAVE` socket** following the **28-pin / 32-pin SVG diagram** (notch facing UP toward LCD, bottom edge flush with the lever side).
4. Drag & drop a `.bin` or `.nes` file into section 3:
   - For `.nes` files (iNES / NES 2.0), PRG and CHR banks are automatically split into clickable buttons — choose the bank you want to write.
   - If the binary is smaller than the chip (e.g., 8 KB CHR on a 32 KB `27C256`), `Mirror repeat` padding is automatically selected.
5. Run **Blank Check** (or **Read IC** first if backing up an existing chip), then click **Program (Write)** to run Blank Check → Program → Verify with live progress and logs.

#### Option B: Desktop GUI (`FireMinipro`)
1. Launch `open -a /Applications/fireminipro.app`.
2. Select **`LEAPER-3C`** as the programmer (click **Rescan** if needed) and choose your IC from **Device**.
3. Use **Load file to buffer** / **Read from target** / **Blank check** / **Write to target**.
4. Language (`System Default` / `English` / `日本語`) and 5 themes (`Famicom`, `NES`, `Light`, `Gray`, `Dark`) can be switched in the separate **Settings** window (bottom-left **Settings** button, `Cmd+,`, or the menu bar).

#### Option C: CLI (`leaper3c`)

| Command | Description |
| --- | --- |
| `./leaper3c info` | Show connected LP-3C firmware version, USB bus/addr, IC count, and status (`0x42`). |
| `./leaper3c list [QUERY] [--vendor V]` | List or search the 260 built-in supported ICs (e.g., `27C256`, `39SF010A`, `29F010`). |
| `./leaper3c chip-info <CHIP> [--vendor V]` | Select chip (`0x4B`) and display exact capacity, 512B blocks, and Vcc/Vpp values. |
| `./leaper3c blank-check <CHIP> [--vendor V]` | Check whether the IC in the Slave socket is blank (`0xFF`). |
| `./leaper3c read <CHIP> -o <OUT.bin> [--vendor V]` | Read entire IC to a binary file with progress and `SUM16` / `CRC32` / `SHA256`. |
| `./leaper3c verify <CHIP> -i <IN.bin> [--vendor V] [--pad none\|ff\|mirror]` | Read back IC and compare byte-for-byte against a local binary file. |
| `./leaper3c erase <CHIP> [--vendor V]` | Electrically erase Flash / EEPROM chips (disabled for UV-EPROMs). |
| `./leaper3c program <CHIP> -i <IN.bin> [--vendor V] [--pad ...] [--erase] [--dry-run] [-y]` | Program IC (Blank Check -> Program -> Read-Back Verify). |
| `./leaper3c split-nes <ROM.nes> [-o DIR] [--prg-pad-kb N] [--chr-pad-kb N]` | Split `.nes` ROM into `.prg.bin` and `.chr.bin` (with optional mirror padding). |
| `./leaper3c ui [--port 8765] [--no-browser]` | Launch the built-in browser UI (`.nes` splitter, 28P/32P socket SVG diagram, Hex viewer, 5 themes). |

### Examples

```sh
# Check programmer connection
./leaper3c info

# Search for 27C series EPROMs or SST39SF series Flash
./leaper3c list 27C
./leaper3c list 39SF

# Inspect chip parameters (specify --vendor when multiple vendors share the same part name)
./leaper3c chip-info 27C256 --vendor 27XXX
./leaper3c chip-info 39SF010A --vendor SST

# Back up an existing 27C256 EPROM
./leaper3c read 27C256 --vendor 27XXX -o backup_27c256.bin

# Split a NES ROM (NROM 32KB PRG + 8KB CHR)
./leaper3c split-nes game.nes -o ./bins

# Preview programming (no hardware write performed)
./leaper3c program 27C256 --vendor 27XXX -i ./bins/game.prg.bin --dry-run

# Erase + Blank Check + Program + Verify on Flash (mirroring 32KB PRG into 128KB SST39SF010A)
./leaper3c program 39SF010A --vendor SST -i ./bins/game.prg.bin --pad mirror --erase

# Program + Verify a UV-erased 27C256 (PRG) and 27C64 (CHR)
./leaper3c program 27C256 --vendor 27XXX -i ./bins/game.prg.bin
./leaper3c program 27C64 --vendor 27XXX -i ./bins/game.chr.bin
```

### USB Host Protocol Overview (LP-3C Firmware 1.30 Interoperability)

- **USB IDs**: `idVendor = 0x04CE`, `idProduct = 0x0A3C`, Configuration `1`, Interface `0` (`EP 0x81` Bulk IN, `EP 0x02` Bulk OUT).
- **Vendor Control Transfers**:
  - `0xC0 / 0x41` (`len=22`): Read firmware banner (`"  FLASH WRITER  1.30"`) + IC table base address (`0xD61E`).
  - `0xC0 / 0x42` (`len=8`): Read hardware status (`status[0] == 0` ready/pass; `status[1] == 1` RUN FAIL; `status[1] == 8` OVER CURRENT FAIL).
  - `0xC0 / 0x44` (`wValue=addr, len=64`): Read internal RAM / IC database (`0xD61E..0xDF82`) or 512B block buffer (`0x0800..0x09FF`).
  - `0xC0 / 0x4B` (`wValue=vendor_id, wIndex=dev_id, len=34`): Select active chip and read 34-byte chip parameters (capacity, Vcc/Vpp).
  - `0x40 / 0x47` (`wValue=0x800..0x980, wIndex=0x80`): Prepare 128-byte buffer offset followed by Bulk OUT transfer on `EP 0x02`.
  - `0x40 / 0x49`: Command dispatch (`wIndex`: `0x50 'P'` session start, `0x52 'R'` session end, `0x54 'T'` program start, `0x57 'W'` read 512B block, `0x59 'Y'` blank check, `0x5B '['` program 512B block, `0x5C '\'` erase, `0x5D ']'` verify). When a command targets `(vendor_id, dev_id)`, `wValue=dev_id` and `wLength=vendor_id`.

### Files in This Repository
- `leaper3c.py` — Standalone macOS USB driver & CLI implementation
- `leaper3c` — Portable shell wrapper (auto-selects `./venv/bin/python3`, `./.venv/bin/python3`, or system `python3`)
- `leaper3c_ui.py` — Built-in local Web GUI (`./leaper3c ui`) with `.nes` splitter, Hex preview & 28P/32P SVG socket guide
- `minipro_shim.py` / `minipro` — `minipro`-compatible CLI bridge for open-source GUIs such as FireMinipro
- `install_fireminipro_bridge.sh` — Helper to link `/opt/homebrew/bin/minipro` and patch `FireMinipro.app`
- `test_leaper3c.py` — Unit tests (chip size decoding, image padding, mock USB roundtrip, over-current handling, iNES splitting, `minipro` shim, Web UI API)
- `requirements.txt` — Python dependencies (`pyusb`)

### Running Tests

```sh
./venv/bin/python3 -m unittest discover -v
```

---

<a id="日本語"></a>
## 日本語

**LEAP Electronic 製 ROM ライタ「LEAPER-3C (LP-3C FLASH WRITER)」**（`USB VID=0x04CE, PID=0x0A3C`）を、macOS（Apple Silicon / Intel）から直接操作するためのユーザー空間 USB ドライバ（`leaper3c.py`）、**標準内蔵ブラウザ Web UI（`leaper3c ui`）**、および CLI ツール（`leaper3c`）です。

> **ブラウザ Web UI が標準で付属しているため、コマンドを打ち込まなくても `leaper3c ui` を開くだけで、Web UI 上からそのまま EPROM / Flash への書込み（焼き付け）・ブランクチェック・読出し・ベリファイ・`.nes` 分割が行えます。**

Windows 仮想環境や Boot Camp、macOS カーネル拡張（`.kext`）は不要で、`libusb` + `PyUSB` 経由で直接 USB 通信を行います（デスクトップアプリの FireMinipro 連携にも対応）。

### 主な特徴
- **Web UI（`leaper3c ui`）標準搭載でブラウザからそのまま焼ける**: 追加アプリ不要で `http://127.0.0.1:8765/` のローカル Web UI が起動し、**書込み（プログラム）・ブランクチェック・読出し・ベリファイ・消去**、28P/32P ソケット挿し方 SVG 図解、`.nes` の PRG/CHR 自動分割＆ミラー埋め、HEX ダンプ表示、日本語/English 切替、5 テーマ（ファミコン / NES / ライト / グレー / ダーク）をブラウザから直感的に操作できます。
- **Windows 不要で Mac から完結**: LP-3C（Firmware `1.30`）実機との相互運用性を目的とした、完全独立のオープンソース macOS ユーザー空間 USB ホスト実装です。メーカー公式のソフトウェア・ドライバ・ファームウェア等の著作物やバイナリは一切含みません。
- **本体内蔵 IC データベース対応**: ファームウェア情報（`1.30`）および内蔵 260 種類のメーカー・IC リストを本体から直接取得・検索できます（`info`, `list`, `chip-info`）。
- **書き込み・読み出し・照合を一通りサポート**: `blank-check`、`erase`（Flash/EEPROM）、`read`、`verify`、`program` に対応（512B ブロック進捗表示、`CRC32` / `SHA256` / `SUM16` 出力、`--dry-run` あり）。
- **安全保護機能**:
  - ステータスレジスタ（`0x42`）を監視し、`OVER CURRENT FAIL`（過電流・逆挿し等）や `RUN FAIL` を検知すると即座に停止します。
  - UV-EPROM（`27C64`〜`27C080` など紫外線消去型）に対する誤った電気消去コマンド（`erase`）をブロックします。
- **レトロゲーム ROM 分割補助 (`split-nes`)**: iNES / NES 2.0 形式の `.nes` ファイルから PRG / CHR バイナリを抽出し、大容量チップへ書き込む際の `--pad mirror` / `--pad ff` 拡張にも対応しています。

### ハードウェア使用上の注意
1. **12V 電源アダプタ必須**: 書き込み時は必ず本体に **DC 12V / 500mA AC アダプタ**（または電池）を接続し、電源スイッチを ON にしてください（USB バスパワーだけでは書き込み電圧 Vpp が生成されません）。
2. **使うソケットは右側（Slave Textool）**: PC 接続モードでは **右側の ZIF ソケット（Slave Textool）** を使用します。左側の `Master Textool` はスタンドアロンコピーのコピー元専用です。
3. **IC の向き・ピン位置（下端＝レバー側揃え）**: 本体天面の図（`32` / `28` の IC package diagram）に従い、**切り欠き（1番ピン側）を LCD 側（上向き）** に向けたうえで、**IC の底辺をソケットの下端（レバー側）にぴったり揃えて** 挿入してください。**28ピン IC**（`27C256`, `27C512`, `27C64` 等）を 32ピンソケットへ挿す場合は、**上端（LCD 側）の左右 2 ピンずつを空けて下詰め** にするのが正解です。
4. **動作確認範囲**: macOS（Apple Silicon）＋ 実機 LEAPER-3C（Firmware `1.30`）にて `info` / `list` / `chip-info` / `blank-check` / `read` / `verify` / `split-nes` に加え、**実物 `27C256` UV-EPROM（32 KB）への実書き込み（17.75 秒）＋再読出し 100% バイト一致まで実機確認済み** です。Flash/EEPROM 用の電気消去（`erase`）はモック USB 単体テストでのみ検証しているため、実チップ書き込み前は `--dry-run` での事前確認をおすすめします。

### 動作確認済みの環境（実機テストした Mac のスペック）
- **機種**: MacBook Air (`MacBookAir10,1`) / Apple M1 (`arm64`, 8コア) / メモリ 16 GB
- **OS**: macOS 26.6.2 (`Build 25G83`)
- **実行環境**: `libusb 1.0.30` (Homebrew) / `Python 3.11.3` / `pyusb 1.3.1` / `Qt 6.12.0`（日本語化 FireMinipro）
- **実機ライター**: LEAP Electronic `LEAPER-3C`（`VID=0x04CE, PID=0x0A3C`, Firmware `1.30`）
- **Windows / Linux での利用について**: USB 通信コア（`leaper3c.py`）自体は汎用的な `PyUSB` + `libusb` で書かれていますが、現在のラッパー（`.dylib` 探索・シェルスクリプト・`FireMinipro.app` 連携）は **macOS 専用構成** です（Windows 10/11 等で動かす場合は `libusb-1.0.dll` + Zadig の `WinUSB` ドライバと `.bat` ラッパー対応が別途必要になります）。

### インストール方法（macOS）

1. **必要ライブラリの導入とリポジトリ取得**:
```sh
# 1. Homebrew で libusb をインストール（Apple Silicon の /opt/homebrew へ入ります）
brew install libusb

# 2. リポジトリを取得
git clone https://github.com/p0lyg0n/LEAPER3CforMAC.git
cd LEAPER3CforMAC

# 3. Python 仮想環境を作成して PyUSB をインストール
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

2. **コマンドリンク作成＆GUI・ランチャーアプリ設定（推奨）**:
```sh
# /opt/homebrew/bin/leaper3c と /opt/homebrew/bin/minipro を作成します
# /Applications/fireminipro.app がある場合は LEAPER-3C 用ブリッジ設定も自動で行います
./install_fireminipro_bridge.sh

# （推奨）アプリケーションフォルダに "/Applications/LEAPER-3C Web UI.app" を作成します
# Finder や Launchpad / Spotlight からクリックするだけで Web UI 起動＋ブラウザ表示ができます
./install_webui_app.sh
```
> これを実行すると、次回以降どのディレクトリからでも `leaper3c` / `leaper3c ui` を直接実行できるようになります。

3. **実機接続テスト**:
LEAPER-3C 本体に **DC 12V AC アダプタ** を接続して電源スイッチを ON にし、Mac と USB 接続した状態で次を実行します:
```sh
leaper3c info
# （install_fireminipro_bridge.sh をスキップした場合は ./leaper3c info）
```
ファームウェアバージョン（例: `1.30`）と対応 IC 数（`260`）が表示されれば準備完了です。

4. **（任意）デスクトップアプリ [FireMinipro](https://github.com/Jartza/fireminipro) を使う場合**:
   - `/Applications/fireminipro.app` を配置した状態で `./install_fireminipro_bridge.sh` を実行すると、そのまま `LEAPER-3C` を認識して使えます。
   - 日本語 UI・5 カラーテーマ・別ウィンドウ設定に対応したフォーク版は [`p0lyg0n/fireminipro`（`feat/themes` ブランチ）](https://github.com/p0lyg0n/fireminipro/tree/feat/themes) を Qt6 + CMake でビルドして利用できます（上流へ [PR #22](https://github.com/Jartza/fireminipro/pull/22) 提出済み）。

---

### 使い方（3通りの操作方法）

#### 方法1: 専用ローカル Web UI（アプリクリック起動 / `leaper3c ui`）で使う（一番手軽・おすすめ）
1. 12V 電源＋USB を繋いだ状態で、**アプリケーションフォルダの `LEAPER-3C Web UI` をダブルクリック**（`./install_webui_app.sh` で作成済み）するか、ターミナルから次を実行します:
   ```sh
   leaper3c ui
   ```
   自動でブラウザが開き、`http://127.0.0.1:8765/` が表示されます。
2. 右上の表示が **接続済み (FW 1.30)** になっていることを確認します。右上のドロップダウンから **言語（日本語 / English）** と **5 テーマ（ファミコン / NES / ライト / グレー / ダーク）** を切り替えられます。
3. 左上の検索欄から **ターゲット IC**（例: `27C256 @ 27XXX`）を選び、左下の **「スレーブソケット挿し方ガイド」の SVG 図** を見ながら、**右側「SLAVE」ソケットに下詰め（レバー側揃え・切り欠き上）** でチップを挿します。
4. 右側のファイル枠へ `.bin` または `.nes` ファイルをドラッグ＆ドロップ（またはクリック選択）します:
   - **`.nes` ファイル** の場合は **PRG / CHR が自動分割されてボタン表示** されるため、書き込みたい側のボタンを押します。
   - IC 容量より小さいデータ（例: 8KB CHR → 32KB `27C256`）を読み込んだ場合は、`サイズ不一致時の余白埋め` が自動で `ミラー繰り返し` に切り替わります。
5. まず **`ブランクチェック`**（または既存チップ吸出し時は **`ICから読出し`**）で正常動作を確認したあと、**`書込み（プログラム）`** をクリックすると、事前ブランクチェック → 書込み → ベリファイ照合まで進捗バー＆ログ付きで完了します。

#### 方法2: デスクトップ GUI（FireMinipro）で使う
1. `open -a /Applications/fireminipro.app` で起動します。
2. 左上の **プログラマ** に `LEAPER-3C`（出ない場合は **再検索**）、**IC型番** に書き込みたいチップを選びます。
3. **ファイル読込** / **ICから読出し** / **ブランクチェック** / **ICへ書込み** ボタンで操作します。
4. 左下の **設定** ボタン（または上部メニューバー・`⌘,`）から別ウィンドウを開き、言語や 5 種類のカラーテーマをいつでも切り替えられます。

#### 方法3: コマンドライン（CLI）で使う

| コマンド | 内容 |
| --- | --- |
| `./leaper3c info` | 接続中の本体ファームウェア、USB接続先、内蔵IC数、ステータス（`0x42`）を表示 |
| `./leaper3c list [検索文字] [--vendor V]` | 内蔵 260 種類の対応 IC 一覧を表示・検索（例: `27C256`, `39SF010A`） |
| `./leaper3c chip-info <型番> [--vendor V]` | 指定 IC（`0x4B`）を選択し、容量・512B ブロック数・Vcc/Vpp 情報を表示 |
| `./leaper3c blank-check <型番> [--vendor V]` | 右側ソケットの IC が空き状態（全 `0xFF`）かチェック |
| `./leaper3c read <型番> -o <出力.bin> [--vendor V]` | IC の内容を読み出してファイル保存（`SUM16` / `CRC32` / `SHA256` 表示） |
| `./leaper3c verify <型番> -i <比較元.bin> [--vendor V] [--pad none\|ff\|mirror]` | IC を読み出してローカルファイルと 1 バイト単位で照合 |
| `./leaper3c erase <型番> [--vendor V]` | Flash / EEPROM を電気消去（UV-EPROM は安全のため拒否） |
| `./leaper3c program <型番> -i <書込.bin> [--vendor V] [--pad ...] [--erase] [--dry-run] [-y]` | 書き込み実行（Blank Check → Program → Verify） |
| `./leaper3c split-nes <ROM.nes> [-o DIR] [--prg-pad-kb N] [--chr-pad-kb N]` | `.nes` ROM から PRG / CHR バイナリを抽出（ミラー拡張オプション付き） |
| `./leaper3c ui [--port 8765] [--no-browser]` | 内蔵ローカルブラウザUIを起動（`.nes`分割・28P/32P挿し方図・HEXプレビュー・5テーマ対応） |

### 実行例

```sh
# 接続とファームウェアを確認
./leaper3c info

# 27C系 EPROM / SST39SF系 Flash を検索
./leaper3c list 27C
./leaper3c list 39SF

# IC パラメータ確認（同名型番が複数メーカーにある場合は --vendor を指定）
./leaper3c chip-info 27C256 --vendor 27XXX
./leaper3c chip-info 39SF010A --vendor SST

# 既存 ROM のバックアップ読出し
./leaper3c read 27C256 --vendor 27XXX -o backup_27c256.bin

# .nes ROM から PRG (32KB) / CHR (8KB) を分割
./leaper3c split-nes game.nes -o ./bins

# 書き込まずに事前チェックだけ試す（ドライラン）
./leaper3c program 27C256 --vendor 27XXX -i ./bins/game.prg.bin --dry-run

# 書き換え可能な Flash (SST39SF010A) へ 32KB PRG をミラー配置で消去＋書込み＋ベリファイ
./leaper3c program 39SF010A --vendor SST -i ./bins/game.prg.bin --pad mirror --erase

# UVイレーサーで消去済みの 27C256 (PRG) / 27C64 (CHR) へ書込み＋ベリファイ
./leaper3c program 27C256 --vendor 27XXX -i ./bins/game.prg.bin
./leaper3c program 27C64 --vendor 27XXX -i ./bins/game.chr.bin
```

### USB ホスト通信仕様（LP-3C Firmware 1.30 互換通信）

- **USB ID**: `idVendor = 0x04CE`, `idProduct = 0x0A3C`, Configuration `1`, Interface `0`（`EP 0x81` Bulk IN / `EP 0x02` Bulk OUT）
- **ベンダーコントロール転送**:
  - `0xC0 / 0x41` (`len=22`): ファームウェア文字列（`"  FLASH WRITER  1.30"`）と IC テーブル先頭アドレス（`0xD61E`）を取得
  - `0xC0 / 0x42` (`len=8`): ステータス取得（`status[0] == 0` 正常終了、`status[1] == 1` RUN FAIL、`status[1] == 8` OVER CURRENT FAIL）
  - `0xC0 / 0x44` (`wValue=addr, len=64`): 本体 RAM 読出し（`0xD61E..0xDF82` IC DB / `0x0800..0x09FF` 512B バッファ）
  - `0xC0 / 0x4B` (`wValue=vendor_id, wIndex=dev_id, len=34`): 使用 IC 選択＆34バイトパラメータ取得
  - `0x40 / 0x47` (`wValue=0x800..0x980, wIndex=0x80`): 128B 書込みバッファ先頭指定（続けて `EP 0x02` へ Bulk OUT）
  - `0x40 / 0x49`: コマンド実行（`wIndex`: `0x50 'P'` 開始 / `0x52 'R'` 終了 / `0x54 'T'` 書込み開始 / `0x57 'W'` 512B 読出し / `0x59 'Y'` ブランクチェック / `0x5B '['` 512B 書込み確定 / `0x5C '\'` 消去 / `0x5D ']'` ベリファイ）

### 収録ファイル構成
- `leaper3c.py` — macOS 用 USB ドライバ＆CLI本体
- `leaper3c` — ポータブル実行ラッパー（`./venv/bin/python3` / `./.venv/bin/python3` / システム `python3` を自動検出）
- `leaper3c_ui.py` — 内蔵ブラウザ UI（`./leaper3c ui`、`.nes` 自動分割・HEX プレビュー・ソケット図解付き）
- `minipro_shim.py` / `minipro` — FireMinipro 等のオープンソース GUI と連携するための `minipro` 互換ブリッジ
- `install_fireminipro_bridge.sh` — `/opt/homebrew/bin/minipro` リンク＆`FireMinipro.app` 連携セットアップスクリプト
- `test_leaper3c.py` — 単体テスト（容量計算、パディング、モック USB 送受信、過電流検知、iNES 分割、`minipro` シム、Web UI API）
- `requirements.txt` — Python 依存パッケージ（`pyusb`）

### テストの実行方法

```sh
./venv/bin/python3 -m unittest discover -v
```

