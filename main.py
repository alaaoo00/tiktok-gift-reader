import asyncio
import itertools
import os
import threading
import time
from collections import deque

from flask import Flask, jsonify, request, render_template_string

# ================= الإعدادات =================
DEFAULT_USER = os.environ.get("TIKTOK_USER", "nova_scrolls")
RETRY_SECONDS = 15
# =============================================

app = Flask(__name__)

gift_events = deque(maxlen=300)
counter = itertools.count(1)
lock = threading.Lock()

reconnect_event = threading.Event()

state = {
    "username": DEFAULT_USER,
    "connected": False,
    "message": "جاري بدء التشغيل...",
    "target_gift": "",
    "speech_template": "شكراً {sender} على {gift}",
    "is_active": True,  # جديد: حالة التشغيل/الإيقاف
}

try:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.events import GiftEvent, ConnectEvent, DisconnectEvent
    NEW_API = True
except ImportError:
    from TikTokLive import TikTokLiveClient
    from TikTokLive.types.events import GiftEvent, ConnectEvent, DisconnectEvent
    NEW_API = False


def dig(obj, *paths, default=None):
    for path in paths:
        cur = obj
        for part in path.split("."):
            cur = getattr(cur, part, None)
            if cur is None:
                break
        if cur not in (None, ""):
            return cur
    return default


def add_gift(sender, gift_name, count):
    with lock:
        if not state["is_active"]:
            return  # متوقف - لا نضيف هدايا
        target = state["target_gift"].strip().lower()
        if target and target not in gift_name.strip().lower():
            print(f"⏭️ تجاهل: {sender} - {gift_name}")
            return
        item = {
            "id": next(counter),
            "sender": sender,
            "gift": gift_name,
            "count": count,
            "ts": time.time(),
        }
        gift_events.append(item)
    print(f"🎁 {sender} أرسل {gift_name} ×{count}")


async def handle_gift(event):
    sender = dig(event, "user.nickname", "user.unique_id", "user.display_name", default="صديق")
    gift_name = dig(event, "gift.name", "gift.info.name", "gift.extended_gift.name", default="هدية")
    count = dig(event, "repeat_count", "gift.count", "gift.repeat_count", default=1) or 1

    streakable = bool(dig(event, "gift.streakable", "gift.info.streakable", default=False))
    repeat_end = dig(event, "repeat_end", "gift.repeat_end", default=True)

    if streakable and repeat_end is False:
        return

    try:
        count = int(count)
    except Exception:
        count = 1

    add_gift(str(sender), str(gift_name), count)


async def handle_connect(event):
    with lock:
        state["connected"] = True
        state["message"] = f"متصل ببث @{state['username']} ✅"
    print(state["message"])


async def handle_disconnect(event):
    with lock:
        state["connected"] = False
        state["message"] = "انقطع الاتصال، جاري إعادة المحاولة..."
    print("⚠️ انقطع الاتصال")


def register(client, new_evt, old_name, handler):
    if NEW_API:
        try:
            client.on(new_evt)(handler)
            return
        except Exception:
            pass
        try:
            client.add_listener(new_evt, handler)
            return
        except Exception:
            pass
    else:
        try:
            client.add_listener(old_name, handler)
            return
        except Exception:
            pass
        client.on(old_name)(handler)


async def tiktok_loop():
    while True:
        with lock:
            state["connected"] = False
            state["message"] = f"جاري الاتصال بـ @{state['username']} ..."
        try:
            client = TikTokLiveClient(unique_id=state["username"])
            register(client, GiftEvent, "gift", handle_gift)
            register(client, ConnectEvent, "connect", handle_connect)
            register(client, DisconnectEvent, "disconnect", handle_disconnect)

            try:
                await client.start()
            except TypeError:
                await client.connect()

            with lock:
                state["message"] = "انتهى البث أو انقطع الاتصال."
        except Exception as e:
            msg = str(e)
            with lock:
                state["connected"] = False
                if ("LIVE_NOT_FOUND" in msg or "not live" in msg.lower() or
                    "user_not_found" in msg.lower() or "UserOffline" in msg or
                    "Offline" in msg):
                    state["message"] = f"@{state['username']} غير لايف الآن — شغّل البث وسيتصل تلقائياً."
                else:
                    state["message"] = f"خطأ: {msg[:120]}"
            print("❌", msg)

        with lock:
            state["connected"] = False
        
        reconnect_event.wait(timeout=RETRY_SECONDS)
        reconnect_event.clear()


def run_tiktok_thread():
    asyncio.run(tiktok_loop())


# ------------------ واجهة الويب ------------------
@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/api/state")
def api_state():
    with lock:
        latest = gift_events[-1]["id"] if gift_events else 0
        return jsonify({
            "username": state["username"],
            "connected": state["connected"],
            "message": state["message"],
            "target_gift": state["target_gift"],
            "speech_template": state["speech_template"],
            "is_active": state["is_active"],
            "latest_id": latest,
        })


@app.route("/api/events")
def api_events():
    after = int(request.args.get("after", 0))
    with lock:
        items = [g for g in gift_events if g["id"] > after]
    return jsonify(items)


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(silent=True) or {}
    with lock:
        if "target_gift" in data:
            state["target_gift"] = str(data["target_gift"])[:60]
        if "speech_template" in data:
            state["speech_template"] = str(data["speech_template"])[:200]
    return jsonify({
        "ok": True,
        "target_gift": state["target_gift"],
        "speech_template": state["speech_template"],
    })


@app.route("/api/change_user", methods=["POST"])
def api_change_user():
    data = request.get_json(silent=True) or {}
    new_user = str(data.get("username", "")).strip().lstrip("@")[:50]
    
    if not new_user:
        return jsonify({"ok": False, "message": "اسم المستخدم فارغ"}), 400
    
    with lock:
        state["username"] = new_user
        state["connected"] = False
        state["message"] = f"جاري الاتصال بـ @{new_user} ..."
    
    reconnect_event.set()
    
    print(f"🔄 تغيير الحساب إلى @{new_user}")
    return jsonify({"ok": True, "username": new_user, "message": f"جاري الاتصال بـ @{new_user}"})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    """تبديل حالة التشغيل/الإيقاف"""
    with lock:
        state["is_active"] = not state["is_active"]
        is_active = state["is_active"]
    
    if is_active:
        msg = "✅ تم التشغيل - جاري قراءة الهدايا"
    else:
        msg = "⏸️ تم الإيقاف - لن يتم قراءة الهدايا"
    
    print(msg)
    return jsonify({"ok": True, "is_active": is_active, "message": msg})


HTML_PAGE = """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>قارئ هدايا تيك توك</title>
<style>
  body{font-family:system-ui,Arial;margin:0;padding:16px;background:#111;color:#fff}
  h1{font-size:20px;color:#ff0050;margin:0 0 12px}
  .card{background:#1e1e1e;border-radius:12px;padding:14px;margin-bottom:12px}
  button{background:#ff0050;color:#fff;border:0;border-radius:10px;padding:12px 16px;font-size:16px;width:100%;cursor:pointer}
  button.sec{background:#333;margin-top:8px}
  button.stop{background:#f44336;margin-top:8px}
  button.start{background:#4CAF50;margin-top:8px}
  input,select,textarea{width:100%;padding:12px;border-radius:10px;border:1px solid #444;background:#000;color:#fff;font-size:16px;box-sizing:border-box;margin-top:6px}
  textarea{min-height:70px;resize:vertical;font-family:inherit}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#888;margin-left:6px}
  .on{background:#0f0}.off{background:#f33}
  .row{font-size:15px;padding:8px;border-bottom:1px solid #2a2a2a}
  small{color:#aaa;display:block;margin-top:4px}
  label{font-size:14px;color:#ccc}
  .preset{display:inline-block;background:#2a2a2a;color:#fff;border:1px solid #444;border-radius:8px;padding:6px 10px;margin:3px;font-size:13px;cursor:pointer}
  .preset:hover{background:#ff0050}
  .user-display{font-size:18px;color:#00f2ea;font-weight:bold;margin-top:8px}
  .status-badge{display:inline-block;padding:4px 10px;border-radius:6px;font-size:13px;margin-right:6px}
  .badge-active{background:#4CAF50;color:#fff}
  .badge-stopped{background:#f44336;color:#fff}
</style>
</head>
<body>
<h1>🎁 قارئ هدايا تيك توك</h1>

<div class="card">
  <button id="unlock">🔊 تفعيل الصوت (اضغط مرة واحدة)</button>
  <button class="sec" id="test">تجربة الصوت</button>
  <div style="margin-top:10px">
    <span class="dot" id="dot"></span><span id="status">...</span>
    <span class="status-badge" id="activeBadge">● شغّال</span>
  </div>
  <div class="user-display" id="userDisplay">@nova_scrolls</div>
</div>

<div class="card">
  <b>⏯️ التحكم بالتشغيل</b>
  <button class="stop" id="toggleBtn">⏸️ إيقاف قراءة الهدايا</button>
  <small id="toggleMsg">التطبيق يعمل ويقرأ الهدايا</small>
</div>

<div class="card">
  <b>👤 تغيير حساب تيك توك</b>
  <small>اكتب اسم المستخدم الجديد (بدون @) واضغط حفظ</small>
  <input id="newUser" placeholder="مثال: nova_scrolls">
  <button class="sec" id="changeUser">حفظ الحساب والاتصال</button>
  <small id="userChanged"></small>
</div>

<div class="card">
  <b>🎙️ إعدادات الصوت</b>
  <label>اختر الصوت</label>
  <select id="voiceSelect"></select>
  
  <label style="margin-top:12px">السرعة: <span id="rateVal">0.95</span></label>
  <input type="range" id="rateSlider" min="0.5" max="2" step="0.05" value="0.95" oninput="document.getElementById('rateVal').textContent=this.value">
  
  <label style="margin-top:12px">الحدة (pitch): <span id="pitchVal">1.0</span></label>
  <input type="range" id="pitchSlider" min="0.5" max="2" step="0.1" value="1.0" oninput="document.getElementById('pitchVal').textContent=this.value">
</div>

<div class="card">
  <b>🎯 الهدية المختارة</b>
  <small>اتركها فاضية لقراءة كل الهدايا، أو اكتب اسم هدية محددة</small>
  <input id="target" placeholder="مثال: Rose أو Lion">
  <button class="sec" id="save">حفظ الهدية المختارة</button>
  <small id="saved"></small>
</div>

<div class="card">
  <b>💬 صيغة الكلام المنطوق</b>
  <small>استخدم: <b>{sender}</b> = الاسم، <b>{gift}</b> = الهدية، <b>{count}</b> = العدد</small>
  <textarea id="template" placeholder="شكراً {sender} على {gift}"></textarea>
  
  <div style="margin-top:10px">
    <small>صيغ جاهزة (اضغط للاختيار):</small>
    <div style="margin-top:6px">
      <span class="preset" data-t="شكراً {sender} على {gift}">شكر بسيط</span>
      <span class="preset" data-t="أهلاً وسهلاً {sender}، شكراً على الهدية">ترحيب</span>
      <span class="preset" data-t="واو! {sender} أرسل {count} {gift}! شكراً جزيلاً">حماسي</span>
      <span class="preset" data-t="نشكر {sender} على هدية {gift}">رسمي</span>
      <span class="preset" data-t="Thanks {sender} for the {gift}">إنجليزي</span>
      <span class="preset" data-t="{sender} ❤️ {gift}">قصير</span>
    </div>
  </div>
  
  <button class="sec" id="saveTemplate" style="margin-top:10px">حفظ الصيغة</button>
  <small id="templateSaved"></small>
</div>

<div class="card">
  <b>📋 آخر الهدايا</b>
  <div id="log"></div>
</div>

<script>
let audioReady = false;
let lastId = null;
let arVoice = null;
let voices = [];

function loadVoices(){
  voices = speechSynthesis.getVoices() || [];
  const select = document.getElementById('voiceSelect');
  if(!select) return;
  select.innerHTML = '<option value="">صوت تلقائي (عربي إن أمكن)</option>';
  voices.forEach((v, i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = `${v.name} (${v.lang})`;
    if((v.lang||'').toLowerCase().startsWith('ar')) opt.selected = true;
    select.appendChild(opt);
  });
}
loadVoices();
if(typeof speechSynthesis !== 'undefined') speechSynthesis.onvoiceschanged = loadVoices;

function buildText(template, sender, gift, count){
  return template
    .replace(/{sender}/g, sender)
    .replace(/{gift}/g, gift)
    .replace(/{count}/g, count);
}

function speak(text){
  if(!audioReady) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = 'ar-SA';
  const sel = document.getElementById('voiceSelect');
  if(sel && sel.value !== '') u.voice = voices[parseInt(sel.value)];
  else if(arVoice) u.voice = arVoice;
  u.rate = parseFloat(document.getElementById('rateSlider').value) || 0.95;
  u.pitch = parseFloat(document.getElementById('pitchSlider').value) || 1.0;
  u.volume = 1;
  speechSynthesis.speak(u);
}

document.getElementById('unlock').onclick = async () => {
  audioReady = true;
  speak("تم تفعيل الصوت");
  document.getElementById('unlock').textContent = "✅ الصوت مفعّل";
  try{ if('wakeLock' in navigator) await navigator.wakeLock.request('screen'); }catch(e){}
};

document.getElementById('test').onclick = () => {
  const tpl = document.getElementById('template').value || "شكراً {sender} على {gift}";
  const sample = buildText(tpl, "أحمد", "وردة", 1);
  speak(sample);
};

document.getElementById('save').onclick = async () => {
  const val = document.getElementById('target').value;
  const tpl = document.getElementById('template').value;
  const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_gift:val, speech_template:tpl})});
  const j = await r.json();
  document.getElementById('saved').textContent = j.target_gift ? ("سيقرأ فقط هدية: " + j.target_gift) : "سيقرأ كل الهدايا";
};

document.getElementById('saveTemplate').onclick = async () => {
  const tpl = document.getElementById('template').value;
  const r = await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({speech_template:tpl})});
  const j = await r.json();
  document.getElementById('templateSaved').textContent = "✅ تم حفظ الصيغة: " + j.speech_template;
};

document.getElementById('changeUser').onclick = async () => {
  const newUser = document.getElementById('newUser').value.trim().replace(/^@/, '');
  if(!newUser){
    document.getElementById('userChanged').textContent = "⚠️ اكتب اسم المستخدم أولاً";
    return;
  }
  document.getElementById('userChanged').textContent = "⏳ جاري تغيير الحساب...";
  try{
    const r = await fetch('/api/change_user',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:newUser})});
    const j = await r.json();
    if(j.ok){
      document.getElementById('userChanged').textContent = "✅ تم تغيير الحساب إلى @" + j.username;
      document.getElementById('newUser').value = '';
      document.getElementById('userDisplay').textContent = '@' + j.username;
    } else {
      document.getElementById('userChanged').textContent = "❌ " + j.message;
    }
  }catch(e){
    document.getElementById('userChanged').textContent = "❌ خطأ في الاتصال";
  }
};

document.getElementById('toggleBtn').onclick = async () => {
  try{
    const r = await fetch('/api/toggle',{method:'POST'});
    const j = await r.json();
    if(j.ok){
      updateToggleUI(j.is_active, j.message);
    }
  }catch(e){
    document.getElementById('toggleMsg').textContent = "❌ خطأ في الاتصال";
  }
};

function updateToggleUI(isActive, msg){
  const btn = document.getElementById('toggleBtn');
  const badge = document.getElementById('activeBadge');
  const statusMsg = document.getElementById('toggleMsg');
  
  if(isActive){
    btn.textContent = "⏸️ إيقاف قراءة الهدايا";
    btn.className = "stop";
    badge.textContent = "● شغّال";
    badge.className = "status-badge badge-active";
  } else {
    btn.textContent = "▶️ تشغيل قراءة الهدايا";
    btn.className = "start";
    badge.textContent = "● متوقف";
    badge.className = "status-badge badge-stopped";
  }
  statusMsg.textContent = msg;
}

document.querySelectorAll('.preset').forEach(el => {
  el.onclick = () => {
    document.getElementById('template').value = el.dataset.t;
  };
});

async function refreshState(){
  try{
    const s = await (await fetch('/api/state')).json();
    document.getElementById('status').textContent = s.message;
    document.getElementById('dot').className = 'dot ' + (s.connected ? 'on':'off');
    document.getElementById('userDisplay').textContent = '@' + s.username;
    if(lastId === null) lastId = s.latest_id;
    if(document.getElementById('target').value === "" && s.target_gift)
      document.getElementById('target').value = s.target_gift;
    if(document.getElementById('template').value === "" && s.speech_template)
      document.getElementById('template').value = s.speech_template;
    updateToggleUI(s.is_active, s.is_active ? "التطبيق يعمل ويقرأ الهدايا" : "التطبيق متوقف - لن يقرأ الهدايا");
  }catch(e){}
}

async function poll(){
  if(lastId === null){ await refreshState(); return; }
  try{
    const items = await (await fetch('/api/events?after=' + lastId)).json();
    const tpl = document.getElementById('template').value || "شكراً {sender} على {gift}";
    for(const g of items){
      lastId = g.id;
      const txt = buildText(tpl, g.sender, g.gift, g.count);
      speak(txt);
      const d = document.createElement('div');
      d.className = 'row';
      d.textContent = "🎁 " + g.sender + " — " + g.gift + " ×" + g.count;
      const log = document.getElementById('log');
      log.prepend(d);
      while(log.children.length > 40) log.lastChild.remove();
    }
  }catch(e){}
}

refreshState();
setInterval(refreshState, 5000);
setInterval(poll, 1500);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    threading.Thread(target=run_tiktok_thread, daemon=True).start()
    app.run(host="0.0.0.0", port=8080, threaded=True)
