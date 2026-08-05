# Ghidra Decompiler Telegram Bot

File bhejo -> Ghidra usko analyze karke poora decompile kar ke C code + strings + symbols wapas bhej dega. Fully containerized, Railway.com ke liye ready.

## Features
- EXE / DLL / ELF / Mach-O / APK / firmware / etc. — jitna bhi bada file (Telegram limit: 50 MB upload)
- Output: `decompiled.c` (sab functions ka decompiled C code) + `info.txt` (language, compiler, strings, symbols) — ZIP me
- Ek time me ek analysis (concurrent sabit nahi)
- Optional: sirf specific Telegram users ko access (`ALLOWED_USER_IDS`)

## Local run (test ke liye)
```bash
pip install -r requirements.txt
set GHIDRA_HOME=C:\path\to\ghidra   # windows
export GHIDRA_HOME=/opt/ghidra        # linux
set TELEGRAM_BOT_TOKEN=123:ABC
python bot.py
```

## Railway deploy
1. Ye repo GitHub pe push karo
2. Railway > New Project > Deploy from GitHub repo
3. Environment variables set karo:
   - `TELEGRAM_BOT_TOKEN` = @BotFather se token (zaroori)
   - `ALLOWED_USER_IDS` = tumhara Telegram user ID (optional, security ke liye recommended)
   - `MAX_FILE_MB` = 50 (default)
4. Deploy -> ho gaya. Ghidra image ~1.2 GB, build me 3-6 min lagte hain.

> Note: Ghidra ko memory chahiye. Railway free plan (~512 MB RAM) chhoti files ke liye chalega; badi files ke liye Hobby/Pro plan use karo (1-2 GB RAM). `JAVA_MAX_MEM` env se Ghidra ki heap limit set kar sakte ho.

## Bot usage
- `/start` — info
- File (document) bhejo — analysis shuru
- Har command / analysis ~1-10 min lagta hai, badi files pe zyada
