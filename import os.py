import os
import socket
from collections import deque

import discord

# ==================================================
# 設定
# ==================================================

# トークンはコードに直書きしない。
# PowerShellで以下のように一時設定してから実行する:
# $env:DISCORD_BOT_TOKEN="ここに新しいBotトークン"
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# 対象ユーザーと絵文字の対応
USER_EMOJI_MAPPING = {
    1015814747289034772: "<:emoji_14:1451467109761552424>",
}

# オンライン人数分の絵文字メッセージを送るか
SEND_ONLINE_COUNT_MESSAGE = True

# 絵文字連投が長くなりすぎるのを防ぐ
MAX_EMOJI_REPEAT = 50

# 同じPCでBotを二重起動しないために使うローカルポート
INSTANCE_LOCK_PORT = int(os.getenv("BOT_INSTANCE_LOCK_PORT", "38473"))

# Discordから同じイベントが再送された場合に備えて、直近のメッセージIDを保持する
PROCESSED_MESSAGE_LIMIT = 1000
processed_message_ids: set[int] = set()
processed_message_order: deque[int] = deque()

# プロセス終了まで保持し、二重起動防止用のポートを開けたままにする
instance_lock_socket: socket.socket | None = None


# ==================================================
# 起動前チェック
# ==================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN が設定されていません。"
        "PowerShellで $env:DISCORD_BOT_TOKEN=\"新しいBotトークン\" を実行してから起動してください。"
    )


# ==================================================
# Intents設定
# ==================================================

intents = discord.Intents.default()

# メッセージイベントを受け取る
intents.messages = True

# オンライン人数を数えるために必要
intents.members = True
intents.presences = True

# 今回は本文・URL・添付画像の中身を読まないので False のままでよい
# URLだけの投稿、画像添付つき投稿でも「投稿された事実」に反応するだけなら不要
intents.message_content = False

client = discord.Client(
    intents=intents,
    member_cache_flags=discord.MemberCacheFlags.all()
)


# ==================================================
# 関数
# ==================================================

def acquire_single_instance_lock() -> None:
    """同じPC上でBotが複数起動するのを防ぐ。"""
    global instance_lock_socket

    lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Windowsでは他プロセスによる同じポートの使用を明示的に拒否する
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        lock_socket.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_EXCLUSIVEADDRUSE,
            1,
        )

    try:
        lock_socket.bind(("127.0.0.1", INSTANCE_LOCK_PORT))
        lock_socket.listen(1)
    except OSError as error:
        lock_socket.close()
        raise RuntimeError(
            "Botはすでに起動しています。"
            "先に起動したBotを終了してから、もう一度実行してください。"
        ) from error

    instance_lock_socket = lock_socket


def mark_message_processed(message_id: int) -> bool:
    """未処理のメッセージIDなら記録してTrueを返す。"""
    if message_id in processed_message_ids:
        return False

    processed_message_ids.add(message_id)
    processed_message_order.append(message_id)

    if len(processed_message_order) > PROCESSED_MESSAGE_LIMIT:
        oldest_message_id = processed_message_order.popleft()
        processed_message_ids.discard(oldest_message_id)

    return True

def count_online_humans_in_channel(message: discord.Message) -> int:
    """
    そのチャンネルを見られる人間メンバーのうち、
    Discord上でオフラインではない人数を数える。
    Botは除外する。

    注意:
    - invisible の人は Discord上では offline 扱い
    - 権限やキャッシュ状況によって完全一致しない場合がある
    """
    channel = message.channel

    members = getattr(channel, "members", None)

    # スレッドなどで channel.members が取れない場合は guild.members にフォールバック
    if members is None and message.guild is not None:
        members = message.guild.members

    if not members:
        return 0

    count = 0
    for member in members:
        if member.bot:
            continue

        if member.status != discord.Status.offline:
            count += 1

    return count


async def safe_add_reaction(message: discord.Message, emoji_text: str) -> bool:
    """
    リアクション追加。
    権限不足や絵文字不正でBot全体が落ちないようにする。
    """
    try:
        emoji = discord.PartialEmoji.from_str(emoji_text)
        await message.add_reaction(emoji)
        return True

    except discord.Forbidden:
        print("リアクション権限がありません。Add Reactions / Read Message History / View Channel を確認してください。")
        return False

    except discord.NotFound:
        print("対象メッセージ、チャンネル、絵文字が見つかりません。")
        return False

    except discord.HTTPException as e:
        print(f"Discord APIエラーでリアクションできませんでした: {e}")
        return False


async def send_online_count_message(message: discord.Message, emoji_text: str, online_count: int) -> None:
    """
    オンライン人数分の絵文字をメッセージで送る。
    長すぎる場合は上限で切る。
    """
    if online_count <= 0:
        return

    repeat_count = min(online_count, MAX_EMOJI_REPEAT)
    text = emoji_text * repeat_count

    if online_count > MAX_EMOJI_REPEAT:
        text += f"\nオンライン人数: {online_count}人"

    try:
        await message.reply(text, mention_author=False)

    except discord.Forbidden:
        print("メッセージ送信権限がありません。Send Messages を確認してください。")

    except discord.HTTPException as e:
        print(f"Discord APIエラーでメッセージ送信できませんでした: {e}")


# ==================================================
# イベント
# ==================================================

@client.event
async def on_ready():
    print(f"ログイン完了: {client.user} として稼働中です。")
    print("対象ユーザー:", ", ".join(str(user_id) for user_id in USER_EMOJI_MAPPING.keys()))

    for guild in client.guilds:
        online_count = sum(
            1
            for member in guild.members
            if not member.bot and member.status != discord.Status.offline
        )
        print(f"オンライン人数 [{guild.name}]: {online_count}人")


@client.event
async def on_message(message: discord.Message):
    # Bot自身や他Botには反応しない
    if message.author.bot:
        return

    # DMではなくサーバー内だけ対象
    if message.guild is None:
        return

    # 対象ユーザー以外は無視
    emoji_text = USER_EMOJI_MAPPING.get(message.author.id)
    if emoji_text is None:
        return

    # 同一プロセス内で同じメッセージイベントを二重処理しない
    if not mark_message_processed(message.id):
        print(f"重複イベントを無視しました: message_id={message.id}")
        return

    # 元の投稿へのリアクションは、オンライン人数に関係なく付ける
    reacted = await safe_add_reaction(message, emoji_text)

    # Botを除き、オンラインになっているユーザーだけを数える
    online_count = count_online_humans_in_channel(message)

    # 人数分の絵文字メッセージは、オンラインが0人なら送らない
    if SEND_ONLINE_COUNT_MESSAGE and online_count > 0:
        await send_online_count_message(message, emoji_text, online_count)

    print(
        f"{message.author} の投稿に反応しました。"
        f"オンライン人数={online_count}人, "
        f"reaction={reacted}, emoji={emoji_text}"
    )


if __name__ == "__main__":
    acquire_single_instance_lock()
    client.run(TOKEN)
