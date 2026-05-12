import requests
import time
import threading
import os
import subprocess
import json
from datetime import datetime
from pathlib import Path
import urllib3
urllib3.disable_warnings()

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_TOKEN = os.environ["API_TOKEN"]
GROQ_KEY  = os.environ.get("GROQ_KEY", "")
FONT_SIZE = int(os.environ.get("FONT_SIZE", "16"))

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
active_users = {}
lock = threading.Lock()

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def tg_send(chat_id, text, reply_to=None, thread_id=None):
    p = {"chat_id": chat_id, "text": str(text)[:4096]}
    if reply_to: p["reply_to_message_id"] = reply_to
    if thread_id: p["message_thread_id"] = thread_id
    try: requests.post(f"{TG}/sendMessage", json=p, timeout=15)
    except Exception as e: log(f"tg_send err: {e}")

def run_pipeline(msg_chat, thread_id, msg_id, user_id, heygen_key, avatar_id, voice_id, script_text):
    try:
        session_id = int(time.time())
        log(f"🎬 user={user_id} avatar={avatar_id[:16]} voice={voice_id[:8]}")
        sub_note = " + субтитры" if GROQ_KEY else ""
        tg_send(msg_chat, f"⏳ Генерирую видео{sub_note}...\n🤖 {avatar_id[:30]}\n🕐 3-10 мин.",
            reply_to=msg_id, thread_id=thread_id)

        hg_h = {"X-Api-Key": heygen_key, "Content-Type": "application/json"}
        payload = {
            "video_inputs": [{
                "character": {"type": "avatar", "avatar_id": avatar_id.strip(),
                              "avatar_style": "normal", "motion_engine": "avatar_iii"},
                "voice": {"type": "text", "input_text": script_text.strip(),
                          "voice_id": voice_id.strip(), "speed": 1.0},
                "background": {"type": "color", "value": "#000000"}
            }],
            "dimension": {"width": 720, "height": 1280}
        }

        r = requests.post("https://api.heygen.com/v2/video/generate",
            headers=hg_h, json=payload, timeout=60, verify=False)
        rj = r.json()
        log(f"HeyGen HTTP {r.status_code}: {json.dumps(rj)[:150]}")

        if r.status_code != 200 or rj.get("error"):
            tg_send(msg_chat, f"❌ HeyGen error: {rj}", reply_to=msg_id, thread_id=thread_id); return

        video_id = (rj.get("data") or {}).get("video_id")
        if not video_id:
            tg_send(msg_chat, "❌ No video_id from HeyGen", reply_to=msg_id, thread_id=thread_id); return

        log(f"⏳ Polling {video_id}...")
        start = time.time()
        video_url = None
        for _ in range(120):
            time.sleep(10)
            pr = requests.get("https://api.heygen.com/v1/video_status.get",
                headers=hg_h, params={"video_id": video_id}, timeout=30, verify=False)
            pd = pr.json()
            status = (pd.get("data") or {}).get("status") or pd.get("status", "unknown")
            log(f"  [{int(time.time()-start)}s] {status}")
            if status == "completed":
                video_url = (pd.get("data") or {}).get("video_url"); break
            elif status in ("failed", "error"):
                tg_send(msg_chat, f"❌ HeyGen failed: {(pd.get('data') or {}).get('error','')}",
                    reply_to=msg_id, thread_id=thread_id); return

        if not video_url:
            tg_send(msg_chat, "❌ Timeout HeyGen", reply_to=msg_id, thread_id=thread_id); return

        log("⬇️ Downloading...")
        raw_path = f"/tmp/hg_{session_id}_raw.mp4"
        vid_bytes = requests.get(video_url, timeout=180, verify=False).content
        Path(raw_path).write_bytes(vid_bytes)
        size_mb = round(len(vid_bytes)/1024/1024, 1)
        log(f"✅ {size_mb} MB")

        final_path = raw_path
        if GROQ_KEY:
            tmp_audio = f"/tmp/hg_{session_id}.mp3"
            subprocess.run(["ffmpeg","-y","-i",raw_path,"-vn","-acodec","libmp3lame",
                "-ar","16000","-ac","1","-ab","64k",tmp_audio], capture_output=True)
            with open(tmp_audio,"rb") as af:
                resp = requests.post("https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_KEY}"},
                    files={"file": ("a.mp3", af, "audio/mpeg")},
                    data={"model":"whisper-large-v3-turbo","response_format":"verbose_json",
                          "timestamp_granularities[]":"word"}, timeout=120)
            words = resp.json().get("words",[]) if resp.status_code==200 else []
            log(f"Transcribed: {len(words)} words")

            if words:
                tmp_ass = f"/tmp/hg_{session_id}.ass"
                def ts(s):
                    h=int(s//3600);m=int((s%3600)//60);sc=s%60
                    return f"{h}:{m:02d}:{sc:05.2f}"
                chunks=[]; i=0
                while i<len(words):
                    c=words[i:i+4]
                    chunks.append((c[0].get("start",0),c[-1].get("end",0),
                                   " ".join(w.get("word","").strip() for w in c)))
                    i+=4
                font_path="/app/fonts/BrownVolky.ttf"
                fname="Brown Volky" if Path(font_path).exists() else "Arial"
                ass=f"""[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{fname},{FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,1,0,1,2.5,0,2,30,30,60,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
                for st,en,ln in chunks:
                    ass+=f"Dialogue: 0,{ts(st)},{ts(en)},Default,,0,0,0,,{ln.strip()}\n"
                Path(tmp_ass).write_text(ass,encoding="utf-8")
                final_path=f"/tmp/hg_{session_id}_final.mp4"
                rb=subprocess.run(["ffmpeg","-y","-i",raw_path,"-vf",
                    f"ass={tmp_ass}","-c:v","libx264","-crf","18","-preset","fast",
                    "-c:a","copy",final_path], capture_output=True, text=True)
                if rb.returncode==0:
                    size_mb=round(Path(final_path).stat().st_size/1024/1024,1)
                    log(f"✅ Subtitles done! {size_mb}MB")
                    for f in [tmp_audio,tmp_ass,raw_path]:
                        try: Path(f).unlink(missing_ok=True)
                        except: pass
                else:
                    log(f"⚠️ ffmpeg err: {rb.stderr[-100:]}"); final_path=raw_path

        tg_caption = script_text[:200].strip()
        with open(final_path,"rb") as f:
            tr = requests.post(f"{TG}/sendVideo",
                data={"chat_id":msg_chat,"caption":tg_caption},
                files={"video":(f"v_{session_id}.mp4",f,"video/mp4")}, timeout=300)
        td = tr.json()
        log(f"TG ok={td.get('ok')} err={td.get('description','')}")
        if not td.get("ok"):
            tg_send(msg_chat,f"❌ TG failed: {td.get('description','')}",reply_to=msg_id,thread_id=thread_id)
        try: Path(final_path).unlink(missing_ok=True)
        except: pass
    except Exception as e:
        import traceback
        log(f"❌ {e}\n{traceback.format_exc()[-300:]}")
        tg_send(msg_chat,f"❌ {str(e)[:200]}",reply_to=msg_id,thread_id=thread_id)
    finally:
        with lock: active_users[user_id]=False

HELP = ("👋 HeyGen Bot\n\n"
        "Формат:\n/gen HEYGEN_KEY|AVATAR_ID|VOICE_ID\nТекст\n\n"
        "Пример:\n/gen sk_V2_xxx|dc0778...|034ca0...\nYour text here")

log(f"🚀 HeyGen Bot | subtitles={'ON' if GROQ_KEY else 'OFF'}")
try:
    requests.post(f"{TG}/deleteWebhook",json={"drop_pending_updates":True},timeout=10)
    requests.post(f"{TG}/setMyCommands",json={"commands":[
        {"command":"gen","description":"Генерировать видео: /gen KEY|AVATAR|VOICE"},
        {"command":"start","description":"Инструкция"}]},timeout=10)
    log("Ready ✅")
except: pass

offset=0
while True:
    try:
        r=requests.post(f"{TG}/getUpdates",
            json={"offset":offset,"timeout":30,"limit":100,
                  "allowed_updates":["message","channel_post"]},timeout=40)
        if r.status_code!=200: time.sleep(5); continue
        updates=r.json().get("result",[])
        if updates: log(f"📨 {len(updates)} update(s)")
        for upd in updates:
            offset=upd["update_id"]+1
            msg=upd.get("message") or upd.get("channel_post")
            if not msg: continue
            mc=str(msg.get("chat",{}).get("id",""))
            mi=msg.get("message_id")
            ti=msg.get("message_thread_id")
            ui=str((msg.get("from") or {}).get("id","unknown"))
            tx=(msg.get("text") or "").strip()
            log(f"  chat={mc} user={ui} text={repr(tx[:50])}")
            if not tx: continue
            if tx.startswith("/start"): tg_send(mc,HELP,thread_id=ti); continue
            if tx.lower().startswith("/gen"):
                parts=tx.split(None,1); body=parts[1].strip() if len(parts)>1 else ""
                if not body: tg_send(mc,"❌ /gen KEY|AVATAR|VOICE\nТекст",reply_to=mi,thread_id=ti); continue
                lines=body.split("\n",1); fl=lines[0].strip(); sc=lines[1].strip() if len(lines)>1 else ""
                params=[p.strip() for p in fl.split("|")]
                if len(params)!=3: tg_send(mc,"❌ Первая строка: KEY|AVATAR_ID|VOICE_ID",reply_to=mi,thread_id=ti); continue
                hk,av,vo=params
                if not sc: tg_send(mc,"❌ Добавь текст со второй строки",reply_to=mi,thread_id=ti); continue
                with lock:
                    if active_users.get(ui): tg_send(mc,"⏳ Твоё видео генерируется.",reply_to=mi,thread_id=ti); continue
                    active_users[ui]=True
                threading.Thread(target=run_pipeline,args=(mc,ti,mi,ui,hk,av,vo,sc),daemon=True).start()
    except Exception as e:
        log(f"Poll error: {e}"); time.sleep(5)
