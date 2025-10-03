from telegram import Update, Bot
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
import logging
import os
import re

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory games storage
# Key: group_chat_id
# Value: dict with keys: 'players' (list of user_id), 'player_names' (map id->name), 'secrets' (map id->secret or None), 'turn_index' (0/1), 'finished'
GAMES = {}

SECRET_RE = re.compile(r'^[1-9](?!.*\1)[1-9](?!.*\2)[1-9](?!.*\3)[1-9](?!.*\4)$')
# More explicit: we will validate programmatically ensuring 4 digits, each 1-9, no repeats

def valid_secret(num: str) -> bool:
    if not num.isdigit() or len(num) != 4:
        return False
    digits = list(num)
    if '0' in digits:
        return False
    if len(set(digits)) != 4:
        return False
    return True

def compare_guess(secret: str, guess: str):
    # returns (values, positions)
    values = sum(1 for d in guess if d in secret)
    positions = sum(1 for i in range(4) if guess[i] == secret[i])
    return values, positions

# --- Command handlers ---

def start(update: Update, context: CallbackContext):
    update.message.reply_text("Hi! I'm the NVP bot. Use /newgame in a group to start a 2-player NVP game.")


def newgame(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type == 'private':
        update.message.reply_text("Please run /newgame in a GROUP chat where you want to play.")
        return
    chat_id = chat.id
    if chat_id in GAMES and not GAMES[chat_id].get('finished', True):
        update.message.reply_text("A game is already active in this group. Use /cancel to cancel it.")
        return
    GAMES[chat_id] = {
        'players': [],
        'player_names': {},
        'secrets': {},
        'turn_index': 0,
        'finished': False
    }
    update.message.reply_text("New NVP game created! Players: 0/2. Join with /join")


def join(update: Update, context: CallbackContext):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == 'private':
        update.message.reply_text("You must join from the group chat where /newgame was used.")
        return
    chat_id = chat.id
    if chat_id not in GAMES or GAMES[chat_id].get('finished', True):
        update.message.reply_text("No active game here. Start one with /newgame")
        return
    game = GAMES[chat_id]
    if user.id in game['players']:
        update.message.reply_text("You already joined the game.")
        return
    if len(game['players']) >= 2:
        update.message.reply_text("Game already has 2 players.")
        return
    game['players'].append(user.id)
    game['player_names'][user.id] = user.first_name
    game['secrets'][user.id] = None
    update.message.reply_text(f"{user.first_name} joined the game! Players: {len(game['players'])}/2")
    if len(game['players']) == 2:
        p1 = game['player_names'][game['players'][0]]
        p2 = game['player_names'][game['players'][1]]
        update.message.reply_text(f"Two players joined: {p1} and {p2}.\nEach player, DM me your secret with /secret <4-digit> (digits 1-9, no repeats, no 0).")


def secret(update: Update, context: CallbackContext):
    # This must be sent in private chat to the bot
    chat = update.effective_chat
    user = update.effective_user
    if chat.type != 'private':
        update.message.reply_text("Send your secret privately to me. Use /secret <4-digit> in a private chat with the bot.")
        return
    if len(context.args) != 1:
        update.message.reply_text("Usage: /secret 4567")
        return
    num = context.args[0].strip()
    if not valid_secret(num):
        update.message.reply_text("Invalid secret. It must be 4 digits long, digits 1-9, no repeats, no 0. Example: 4567")
        return
    # Find an active game where this user is a player and their secret is not yet set
    games_for_user = []
    for gid, g in GAMES.items():
        if user.id in g.get('players', []) and not g.get('finished', True) and g['secrets'].get(user.id) is None:
            games_for_user.append((gid, g))
    if not games_for_user:
        update.message.reply_text("Could not find an active game where you need to set a secret. Make sure you joined a group game and the game is active.")
        return
    # If multiple, choose the most recent one (first in dict iteration)
    gid, game = games_for_user[0]
    game['secrets'][user.id] = num
    update.message.reply_text(f"Secret for game in group {gid} saved. Do not reveal it to your opponent.")
    # If both secrets set, announce in group
    if all(game['secrets'].get(pid) for pid in game['players']):
        chat_title = context.bot.get_chat(gid).title or str(gid)
        p1 = game['player_names'][game['players'][0]]
        p2 = game['player_names'][game['players'][1]]
        context.bot.send_message(chat_id=gid, text=f"Both players have set their secrets. Game between {p1} and {p2} starts now! {p1} goes first. Make a guess with /guess <4-digit>.")


def guess(update: Update, context: CallbackContext):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == 'private':
        update.message.reply_text("You should make guesses in the GROUP chat where the game is happening.")
        return
    chat_id = chat.id
    if chat_id not in GAMES or GAMES[chat_id].get('finished', True):
        update.message.reply_text("No active game in this group. Start one with /newgame")
        return
    game = GAMES[chat_id]
    if user.id not in game['players']:
        update.message.reply_text("You are not a player in the current game.")
        return
    if len(context.args) != 1:
        update.message.reply_text("Usage: /guess 7364")
        return
    guess_num = context.args[0].strip()
    if not valid_secret(guess_num):
        update.message.reply_text("Invalid guess. It must be 4 digits long, digits 1-9, no repeats, no 0.")
        return
    # Check both secrets set
    if not all(game['secrets'].get(pid) for pid in game['players']):
        update.message.reply_text("Waiting for both players to privately set their secrets with /secret.")
        return
    # Check turn
    current_player_id = game['players'][game['turn_index']]
    if user.id != current_player_id:
        cur_name = game['player_names'][current_player_id]
        update.message.reply_text(f"It's not your turn. It's {cur_name}'s turn.")
        return
    # Determine opponent
    opponent_id = [pid for pid in game['players'] if pid != user.id][0]
    opponent_name = game['player_names'][opponent_id]
    opponent_secret = game['secrets'][opponent_id]
    values, positions = compare_guess(opponent_secret, guess_num)
    # Which digits are in correct positions? list them
    correct_pos_digits = [guess_num[i] for i in range(4) if guess_num[i] == opponent_secret[i]]
    pos_digits_str = ','.join(correct_pos_digits) if correct_pos_digits else 'none'
    update.message.reply_text(f"{user.first_name} guessed {guess_num} → values={values}, positions={positions} (correct position digits: {pos_digits_str})")
    if positions == 4:
        update.message.reply_text(f"{user.first_name} guessed the secret of {opponent_name} and wins the game! 🎉")
        game['finished'] = True
        return
    # advance turn
    game['turn_index'] = 1 - game['turn_index']
    next_player = game['player_names'][game['players'][game['turn_index']]]
    update.message.reply_text(f"Now it's {next_player}'s turn.")


def status(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type == 'private':
        update.message.reply_text("Use /status in the group where the game is running to see game status.")
        return
    chat_id = chat.id
    if chat_id not in GAMES:
        update.message.reply_text("No game here.")
        return
    game = GAMES[chat_id]
    if game.get('finished', False):
        update.message.reply_text("No active game (finished). Start a new one with /newgame")
        return
    players = [game['player_names'][pid] for pid in game['players']]
    secrets_set = sum(1 for pid in game['players'] if game['secrets'].get(pid))
    update.message.reply_text(f"Players: {players}. Secrets set: {secrets_set}/2. Next turn index: {game['turn_index']}")


def cancel(update: Update, context: CallbackContext):
    chat = update.effective_chat
    if chat.type == 'private':
        update.message.reply_text("Use /cancel in the group to cancel the active game there.")
        return
    chat_id = chat.id
    if chat_id not in GAMES:
        update.message.reply_text("No active game to cancel.")
        return
    GAMES.pop(chat_id, None)
    update.message.reply_text("Game cancelled.")


def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text("Commands (group): /newgame /join /guess <4-digit> /status /cancel\nCommands (private to bot): /secret <4-digit>")


def main():
    TOKEN = '8266984728:AAHEAjQySxKR53dZm7oCnXEh6mSi993Vh6s'
    if TOKEN == 'REPLACE_WITH_YOUR_BOT_TOKEN':
        logger.warning('No TELEGRAM_BOT_TOKEN set; be sure to set it or replace the placeholder in code before running.')
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(CommandHandler('newgame', newgame))
    dp.add_handler(CommandHandler('join', join))
    dp.add_handler(CommandHandler('secret', secret))
    dp.add_handler(CommandHandler('guess', guess))
    dp.add_handler(CommandHandler('status', status))
    dp.add_handler(CommandHandler('cancel', cancel))
    dp.add_handler(CommandHandler('help', help_cmd))

    updater.start_polling()
    logger.info('NVP bot started. Polling...')
    updater.idle()

if __name__ == '__main__':
    main()