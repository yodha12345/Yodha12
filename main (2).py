import telebot
from telebot import types
import os
import random
import time
import threading
import sqlite3
import hashlib
from datetime import datetime, timedelta

# Initialize Bot
API_TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(API_TOKEN, threaded=True, num_threads=25)

ADMIN_KEY = "Eshu2005aru"
GROUP_ID = -1003746627836 
VERIFY_CHANNEL_ID = -1003786586918

# ===== 1. DATABASE SYSTEM =====
DB_NAME = "quiz_pro_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history 
                 (question_hash TEXT PRIMARY KEY, last_used TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS all_time_stats 
                 (user_id INTEGER PRIMARY KEY, name TEXT, 
                  correct INTEGER DEFAULT 0, wrong INTEGER DEFAULT 0, 
                  skip INTEGER DEFAULT 0, score REAL DEFAULT 0.0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS weak_points 
                 (user_id INTEGER, chapter TEXT, wrong_count INTEGER DEFAULT 0,
                  last_update DATE, PRIMARY KEY (user_id, chapter))''')
    conn.commit()
    conn.close()

def mark_question_used(question_text):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    q_hash = hashlib.md5(question_text.encode('utf-8')).hexdigest()
    c.execute("INSERT OR REPLACE INTO history (question_hash, last_used) VALUES (?, ?)", 
              (q_hash, datetime.now()))
    conn.commit()
    conn.close()

def get_used_hashes_30_days():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    thirty_days_ago = datetime.now() - timedelta(days=30) 
    c.execute("SELECT question_hash FROM history WHERE last_used > ?", (thirty_days_ago,))
    used = {row[0] for row in c.fetchall()}
    conn.close()
    return used

def save_session_to_db(session_scores):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    for uid, stats in session_scores.items():
        c.execute('''INSERT INTO all_time_stats (user_id, name, correct, wrong, skip, score)
                     VALUES (?, ?, ?, ?, ?, ?)
                     ON CONFLICT(user_id) DO UPDATE SET
                     name = excluded.name,
                     correct = all_time_stats.correct + excluded.correct,
                     wrong = all_time_stats.wrong + excluded.wrong,
                     skip = all_time_stats.skip + excluded.skip,
                     score = all_time_stats.score + excluded.score''', 
                  (uid, stats['name'], stats['correct'], stats['wrong'], stats['skip'], stats['score']))
    conn.commit()
    conn.close()

init_db()

# ===== 2. GLOBAL STATE =====
question_bank = {} 
user_state = {}
user_step = {}
selected_chapters = {}
user_scores = {} 
quiz_active = {} 
voted_users = set() 
skipped_this_q = set() 

current_poll_data = {"poll_id": None, "correct_id": None, "max_answers": 3, "skip_count": 0, "voter_count": 0, "chapter": ""}

# ===== 3. HANDLERS =====
@bot.message_handler(commands=['start'])
def welcome(message):
    bot.reply_to(message, "👋 **Yodha Bot is Online!**\nUse /admin to configure the exam.")

@bot.poll_answer_handler()
def handle_poll_answer(poll_answer):
    uid = poll_answer.user.id
    if uid not in user_scores:
        user_scores[uid] = {"name": poll_answer.user.first_name, "correct": 0, "wrong": 0, "skip": 0, "score": 0.0}
    if str(poll_answer.poll_id) == str(current_poll_data["poll_id"]):
        if uid not in skipped_this_q and uid not in voted_users:
            current_poll_data["voter_count"] += 1
            voted_users.add(uid)
            if poll_answer.option_ids[0] == current_poll_data["correct_id"]:
                user_scores[uid]["correct"] += 1
                user_scores[uid]["score"] += 1.0
            else:
                user_scores[uid]["wrong"] += 1
                user_scores[uid]["score"] -= 0.25

@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    uid = call.from_user.id
    if call.data.startswith("skip_"):
        target_pid = call.data.split("_")[1]
        if target_pid != str(current_poll_data["poll_id"]):
            return bot.answer_callback_query(call.id, "❌ Expired")
        if uid in voted_users or uid in skipped_this_q:
            return bot.answer_callback_query(call.id, "⚠️ Already responded!")
        if uid not in user_scores:
            user_scores[uid] = {"name": call.from_user.first_name, "correct": 0, "wrong": 0, "skip": 0, "score": 0.0}
        skipped_this_q.add(uid)
        user_scores[uid]["skip"] += 1
        current_poll_data["skip_count"] += 1
        bot.answer_callback_query(call.id, "⏩ Skipped!")
    elif call.data == "stop_quiz":
        quiz_active[GROUP_ID] = False
        bot.answer_callback_query(call.id, "🛑 Stopping Quiz...")

# ===== 4. QUIZ ENGINE =====
def load_questions():
    subjects = ["biology", "math", "reasoning", "physics", "chemistry"]
    for sub in subjects:
        try:
            path = f"questions/{sub}.txt"
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                current_ch = sub.capitalize()
                for block in [b.strip() for b in text.split("\n\n") if b.strip()]:
                    lines = block.split("\n")
                    for l in lines:
                        if l.lower().startswith("#chapter:"): current_ch = l.split(":")[1].strip()
                    if "Answer:" in block:
                        question_bank.setdefault(sub, {}).setdefault(current_ch, []).append(block)
        except: pass

load_questions()

def run_quiz(chat_id):
    global current_poll_data, skipped_this_q, voted_users
    user_scores.clear()
    data = user_state.get(chat_id)
    if not data:
        return
    sub = data['subject']
    
    # Pre-calculate questions
    all_pool = []
    for ch in data['chapters']: all_pool.extend(question_bank[sub].get(ch, []))
    used_hashes = get_used_hashes_30_days()
    fresh_pool = [q for q in all_pool if hashlib.md5(q.encode('utf-8')).hexdigest() not in used_hashes]
    pool_to_use = fresh_pool if len(fresh_pool) >= data['count'] else all_pool
    
    if not pool_to_use:
        bot.send_message(chat_id, "❌ No questions found for the selected chapters.")
        return
        
    selected = random.sample(pool_to_use, min(data['count'], len(pool_to_use)))
    total_q = len(selected)

    # Admin stop button
    stop_markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🛑 STOP QUIZ", callback_data="stop_quiz"))
    bot.send_message(chat_id, "🚨 **Exam Control Panel**\nAdmin, use this to stop the quiz:", reply_markup=stop_markup)

    # Pre-Exam Instructions
    chapters_text = ", ".join(data['chapters'])
    instr = (f"📋 **EXAM INSTRUCTIONS** 📋\n━━━━━━━━━━━━━━\n"
             f"📚 Subject: {sub.upper()}\n"
             f"📂 Chapters: {chapters_text}\n"
             f"🔢 Total Questions: {total_q}\n"
             f"⏱️ Timer: {data['timer']}s | 👤 Limit: {data['max_answers']}\n"
             f"❌ Negative Mark: -0.25\n━━━━━━━━━━━━━━\n🚀 Starting in 20s...")
    instr_msg = bot.send_message(GROUP_ID, instr)
    time.sleep(20)
    try:
        bot.delete_message(GROUP_ID, instr_msg.message_id)
    except: pass

    for block in selected:
        if not quiz_active.get(GROUP_ID): break
        mark_question_used(block)
        current_poll_data.update({"skip_count": 0, "voter_count": 0})
        skipped_this_q.clear()
        voted_users.clear()
        
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        clean_q = [line for line in lines if not line.lower().startswith("#")][0]
        
        # Raw option parsing
        raw_options = [lines[1][3:], lines[2][3:], lines[3][3:], lines[4][3:]]
        # Safeguard: Truncate options to 97 chars + "..." if they exceed Telegram's 100 character limit
        options = [opt[:97] + "..." if len(opt) > 100 else opt for opt in raw_options]
        
        ans = next((l.split(":")[-1].strip() for l in lines if "Answer:" in l), "A")
        correct_idx = ord(ans) - ord("A")

        try:
            poll_msg = bot.send_poll(GROUP_ID, clean_q, options, type='quiz', 
                                     correct_option_id=correct_idx, is_anonymous=False, 
                                     open_period=data['timer'])
            
            # Background Channel verification
            try:
                bot.send_message(VERIFY_CHANNEL_ID, f"✅ Verification: {clean_q}\nAns: {ans}")
            except: pass

            current_poll_data.update({"poll_id": poll_msg.poll.id, "correct_id": correct_idx})
            skip_btn = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("⏩ Skip", callback_data=f"skip_{poll_msg.poll.id}"))
            btn_msg = bot.send_message(GROUP_ID, "Tap to Skip:", reply_markup=skip_btn)

            start_t = time.time()
            while time.time() - start_t < data['timer']:
                if (current_poll_data["voter_count"] + current_poll_data["skip_count"]) >= data['max_answers']: break
                if not quiz_active.get(GROUP_ID): break
                time.sleep(0.5)

            try:
                bot.delete_message(GROUP_ID, btn_msg.message_id)
                bot.stop_poll(GROUP_ID, poll_msg.message_id)
            except: pass
            time.sleep(1.5)
            
        except telebot.apihelper.ApiTelegramException as e:
            # Safe Fallback: Notify the group if Telegram rejects a question block and continue the loop safely
            bot.send_message(GROUP_ID, f"⚠️ Question skipped due to internal error: {e.description}")
            time.sleep(2)
            continue

    save_session_to_db(user_scores)
    
    report = f"📊 **EXAMINATION LEADERBOARD** 📋\n━━━━━━━━━━━━━━\n"
    sorted_u = sorted(user_scores.values(), key=lambda x: x['score'], reverse=True)
    for i, u in enumerate(sorted_u[:10], 1):
        attended = u['correct'] + u['wrong']
        acc = (u['correct'] / attended * 100) if attended > 0 else 0
        report += (f"{i}. 👤 **{u['name']}**\n"
                   f"✅ {u['correct']} | ❌ {u['wrong']} | ⏩ {u['skip']}\n"
                   f"📝 Attended: {attended} | 🎯 {acc:.1f}%\n"
                   f"🏆 Score: {u['score']:.2f}\n"
                   f"━━━━━━━━━━━━━━\n")
    
    report += "\n👑 **ALL-TIME HALL OF FAME** 👑\n━━━━━━━━━━━━━━\n"
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT name, score FROM all_time_stats ORDER BY score DESC LIMIT 5")
    for i, row in enumerate(c.fetchall(), 1):
        report += f"{i}. {row[0]} — {row[1]:.2f} pts\n"
    conn.close()
    bot.send_message(GROUP_ID, report, parse_mode="Markdown")

# ===== 5. ADMIN HANDLERS =====
@bot.message_handler(commands=['admin'])
def admin(message):
    user_step[message.chat.id] = "admin_key"
    bot.send_message(message.chat.id, "🔑 **Enter Admin Key:**")

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "admin_key")
def check_key(m):
    if m.text == ADMIN_KEY:
        user_step[m.chat.id] = "subject"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("Biology", "Math", "Reasoning", "Physics", "Chemistry")
        bot.send_message(m.chat.id, "✅ Access Granted!\n📚 **Select Subject:**", reply_markup=markup)
    else:
        bot.send_message(m.chat.id, "❌ Wrong Key.")

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "subject")
def sel_sub(m):
    sub = m.text.lower()
    if sub in question_bank:
        user_state[m.chat.id] = {'subject': sub}
        user_step[m.chat.id] = "mode"
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True).add("Mix (All) 🎯", "Chapter-wise 📂")
        bot.send_message(m.chat.id, "🎯 **Select Mode:**", reply_markup=markup)

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "mode")
def sel_mode(m):
    sub = user_state[m.chat.id]['subject']
    if "Mix" in m.text:
        user_state[m.chat.id]['chapters'] = list(question_bank[sub].keys())
        user_step[m.chat.id] = "count"
        bot.send_message(m.chat.id, "🔢 **Count:**", reply_markup=types.ReplyKeyboardRemove())
    else:
        user_step[m.chat.id] = "chapter"
        selected_chapters[m.chat.id] = set()
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for ch in question_bank[sub]: markup.add(ch)
        markup.add("DONE ✅")
        bot.send_message(m.chat.id, "Select chapters:", reply_markup=markup)

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "chapter")
def sel_ch(m):
    if m.text == "DONE ✅":
        user_state[m.chat.id]['chapters'] = list(selected_chapters[m.chat.id])
        user_step[m.chat.id] = "count"
        bot.send_message(m.chat.id, "🔢 **Count:**", reply_markup=types.ReplyKeyboardRemove())
    else:
        selected_chapters[m.chat.id].add(m.text)
        bot.send_message(m.chat.id, f"➕ {m.text}")

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "count")
def sel_count(m):
    try:
        user_state[m.chat.id]['count'] = int(m.text)
        user_step[m.chat.id] = "timer"
        bot.send_message(m.chat.id, "⏱️ **Timer (sec):**")
    except:
        bot.send_message(m.chat.id, "❌ Enter a valid number.")

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "timer")
def sel_timer(m):
    try:
        user_state[m.chat.id]['timer'] = int(m.text)
        user_step[m.chat.id] = "limit"
        bot.send_message(m.chat.id, "👤 **Answer Limit:**")
    except:
        bot.send_message(m.chat.id, "❌ Enter a valid number.")

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "limit")
def sel_limit(m):
    try:
        user_state[m.chat.id]['max_answers'] = int(m.text)
        user_step[m.chat.id] = "ready"
        bot.send_message(m.chat.id, "✅ Ready!", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True).add("START QUIZ 🚀"))
    except:
        bot.send_message(m.chat.id, "❌ Enter a valid number.")

@bot.message_handler(func=lambda m: m.text == "START QUIZ 🚀" and user_step.get(m.chat.id) == "ready")
def start_trigger(message):
    quiz_active[GROUP_ID] = True
    threading.Thread(target=run_quiz, args=(message.chat.id,)).start()

if __name__ == "__main__":
    print("🤖 Bot is starting up...")
    bot.infinity_polling(skip_pending=True)
                  
