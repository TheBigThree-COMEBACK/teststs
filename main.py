import os
import discord
from discord.ext import commands
import asyncio
import random
import datetime
import time
from googletrans import Translator
from collections import deque
import aiohttp
import io
import logging
import concurrent.futures
from typing import Optional, Any, Callable, Tuple

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(message)s")

TOKEN = "MTM4OTg4OTM3Mjg1NzU2OTMwMA.GE0hAW.FomlA-PAWXYVg_lMKmVSbqnYovqcTH0CFJ2OQc"

SEND_INTERVAL = 0.20  # fallback sleep (kept for compatibility)
REACT_INTERVAL = 0.28  # fallback sleep (kept for compatibility)

MAX_REACT_CONCURRENCY = 3
MAX_WEBHOOK_SEND = 5
MAX_COPY_CONCURRENCY = 8
MAX_CLONE_ROLE_TASKS = 8
MAX_CLONE_CHANNEL_TASKS = 15
MAX_CLONE_EMOJI_TASKS = 3
MAX_CLONE_WEBHOOK_TASKS = 6

# Clone retries/backoff
RETRY_LIMIT = 5
INITIAL_BACKOFF = 1.0

# Limits for sending messages (batching)
MESSAGE_BATCH_SIZE = 8
MESSAGE_BATCH_PAUSE = 0.5  # seconds after each batch


# ---------------------------
# Adaptive rate limiter (token-bucket)
# ---------------------------
class AdaptiveRateLimiter:

    def __init__(self,
                 rate_per_sec: float = 5.0,
                 capacity: Optional[float] = None):
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity or rate_per_sec)
        self.tokens = self.capacity
        self.last = time.monotonic()
        self.lock = asyncio.Lock()
        self.penalty_until = 0.0

    async def acquire(self, tokens: float = 1.0):
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last = now
            if now < self.penalty_until:
                # small wait while in penalty window
                await asyncio.sleep(max(0.01, self.penalty_until - now))
            if self.tokens >= tokens:
                self.tokens -= tokens
                return
            needed = tokens - self.tokens
            wait = needed / max(self.rate, 0.0001)
        await asyncio.sleep(wait)
        await self.acquire(tokens)

    async def on_rate_limit(self, retry_after: float):
        now = time.monotonic()
        self.penalty_until = max(self.penalty_until,
                                 now + max(retry_after, 1.0))
        # temporarily slow refill
        old_rate = self.rate
        self.rate = max(0.5, self.rate * 0.5)
        logging.warning(
            f"[RateLimiter] backing off: rate {old_rate} -> {self.rate} for {retry_after}s"
        )

        async def restore():
            await asyncio.sleep(max(retry_after, 1.0))
            self.rate = min(self.capacity, max(self.rate * 2.0, 1.0))
            logging.info(f"[RateLimiter] restored rate to {self.rate}")

        asyncio.create_task(restore())


# Instances used globally
REST_RATE = AdaptiveRateLimiter(rate_per_sec=8.0)
REACTION_RATE = AdaptiveRateLimiter(rate_per_sec=9999.0)

# ---------------------------
# Bot init (discord.py-self)
# ---------------------------
client = commands.Bot(command_prefix=".", self_bot=True)
# remove builtin help so our custom one can register
try:
    client.remove_command("help")
except Exception:
    pass

# aiohttp session reused
aio_session: Optional[aiohttp.ClientSession] = None

# ThreadPoolExecutor for blocking tasks (translator, heavy CPU)
thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)

# ---------------------------
# Global Variables (features)
# ---------------------------
self_react_enabled = False
self_react_emoji = ""
react_targets: dict[int, str] = {}
autobold_enabled = False
ap_running = False
ap_delay = 0.0
ladder_running = False
mimic_enabled = False
translator_enabled = False
translator_target_lang = "en"
translator = Translator()
mimic_queue = deque()
mimic_target_id: Optional[int] = None

# caches
_cached_webhooks: dict[int, list[discord.Webhook]] = {
}  # channel_id -> webhooks list

# semaphores to control concurrency where still useful
webhook_send_semaphore = asyncio.Semaphore(MAX_WEBHOOK_SEND)
copy_semaphore = asyncio.Semaphore(MAX_COPY_CONCURRENCY)

# channel locks (serialize per-channel/DM to avoid per-channel buckets)
_channel_locks: dict[int, asyncio.Lock] = {}


def get_channel_lock(channel_id: int) -> asyncio.Lock:
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock


# ---------------------------
# Queues & workers
# ---------------------------
send_queue: asyncio.Queue = asyncio.Queue()
react_queue: asyncio.Queue = asyncio.Queue()

# Worker helpers: queue items are tuples (callable, args, kwargs, future)
QueueItem = Tuple[Callable[..., Any], Tuple[Any, ...], dict, asyncio.Future]


async def _extract_channel_id_from_bound(func: Callable[..., Any], args: tuple,
                                         kwargs: dict) -> Optional[int]:
    # Try to find a sensible channel id associated with the bound callable
    bound_self = getattr(func, "__self__", None)
    # case: bound_self is a Message -> channel attribute
    if bound_self is not None:
        try:
            ch = getattr(bound_self, "channel", None)
            if ch is not None and getattr(ch, "id", None):
                return ch.id
            # sometimes bound_self itself is a Messageable (e.g., ctx)
            if getattr(bound_self, "id", None):
                return getattr(bound_self, "id")
        except Exception:
            pass
    # fallback: if first arg is a channel-like object
    if args:
        first = args[0]
        try:
            if getattr(first, "id", None):
                return getattr(first, "id")
            if getattr(first, "channel", None) and getattr(
                    first.channel, "id", None):
                return first.channel.id
        except Exception:
            pass
    return None


async def send_worker():
    """Dequeue and execute send/edit/webhook coroutines using adaptive limiter and per-channel locks."""
    while True:
        item: QueueItem = await send_queue.get()
        func, args, kwargs, fut = item
        channel_id = await _extract_channel_id_from_bound(func, args, kwargs)
        try:
            # serialize per-channel if we can identify it
            if channel_id:
                lock = get_channel_lock(channel_id)
                async with lock:
                    await REST_RATE.acquire()
                    res = await _safe_call(func, *args, **kwargs)
            else:
                await REST_RATE.acquire()
                res = await _safe_call(func, *args, **kwargs)

            if not fut.done():
                fut.set_result(res)
        except Exception as e:
            logging.debug(f"[send_worker] exception: {e}")
            if not fut.done():
                fut.set_exception(e)
        finally:
            send_queue.task_done()


async def react_worker():
    """Dequeue reaction tasks and execute them paced by REACTION_RATE."""
    while True:
        item: QueueItem = await react_queue.get()
        func, args, kwargs, fut = item
        try:
            await REACTION_RATE.acquire()
            res = await _safe_call(func, *args, **kwargs)
            if not fut.done():
                fut.set_result(res)
        except Exception as e:
            logging.debug(f"[react_worker] exception: {e}")
            if not fut.done():
                fut.set_exception(e)
        finally:
            react_queue.task_done()


async def enqueue_send(func: Callable[..., Any],
                       *args,
                       wait_result: bool = True,
                       **kwargs) -> Any:
    """Place callable in send_queue and return its result (awaits)."""
    fut = client.loop.create_future()
    await send_queue.put((func, args, kwargs, fut))
    if wait_result:
        return await fut
    return fut


async def enqueue_react(func: Callable[..., Any],
                        *args,
                        wait_result: bool = False,
                        **kwargs) -> Any:
    """Place reaction callable in react_queue. Usually we fire-and-forget (no wait)."""
    fut = client.loop.create_future()
    await react_queue.put((func, args, kwargs, fut))
    if wait_result:
        return await fut
    return fut


# small helper wrapper to call a coroutine/callable with retries via safe_request when appropriate
async def _safe_call(func: Callable[..., Any], *args, **kwargs):
    # If it's an aiohttp/discord coroutine that we should call under safe_request wrapper,
    # we try to use safe_request for better retry/backoff handling. Otherwise direct call.
    try:
        # many discord.py coroutines are awaitable callables; safe_request expects a callable
        return await safe_request(func, *args, **kwargs)
    except Exception:
        # final fallback to direct call (keeps compatibility)
        return await func(*args, **kwargs)


# ---------------------------
# Utilities
# ---------------------------
def box_msg(content: str) -> str:
    return f"```{content}```"


async def boxed_temp_msg(ctx_or_channel, content: str, duration: int = 20):
    try:
        # use enqueue_send so messages are paced
        msg = await enqueue_send(ctx_or_channel.send, box_msg(content))
        await asyncio.sleep(duration)
        # delete via queue (waitless is fine)
        try:
            await enqueue_send(msg.delete, wait_result=False)
        except Exception:
            pass
    except Exception:
        # swallow errors
        pass


def random_case(text: str) -> str:
    return "".join(random.choice([c.lower(), c.upper()]) for c in text)


def format_mimic(text: str) -> str:
    return f'"{random_case(text)}" <-- a retard said this btw'


# safe fetch image utility used by cloning
async def fetch_image(url: Optional[str]) -> Optional[bytes]:
    if not url:
        return None
    global aio_session
    try:
        async with aio_session.get(url) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        logging.debug(f"fetch_image error: {e}")
    return None


# safe_request with retries and backoff to respect rate limits
async def safe_request(coro_callable, *args, **kwargs):
    backoff = INITIAL_BACKOFF
    for attempt in range(RETRY_LIMIT):
        try:
            # call the coroutine/callable
            return await asyncio.wait_for(coro_callable(*args, **kwargs),
                                          timeout=30)
        except asyncio.TimeoutError:
            logging.warning("Request timed out; retrying.")
        except discord.HTTPException as e:
            # Permission errors => don't retry
            try:
                code = getattr(e, "code", None)
                status = getattr(e, "status", None)
            except Exception:
                code = None
                status = None
            if code == 50013:
                logging.error("Permission error.")
                return None
            # Rate limited
            # Try to extract retry_after from exception attributes or from .response if available
            retry_after = getattr(e, "retry_after", None)
            # attempt to read headers if available
            try:
                resp = getattr(e, "response", None)
                headers = getattr(resp, "headers", None)
                if headers and retry_after is None:
                    retry_after = headers.get("Retry-After") or headers.get(
                        "retry-after")
                    if retry_after is not None:
                        retry_after = float(retry_after)
            except Exception:
                headers = None
            if status == 429 or retry_after is not None:
                retry_after = float(
                    retry_after) if retry_after is not None else backoff
                logging.warning(
                    f"Rate limited: sleeping {retry_after}s (attempt {attempt+1})"
                )
                # inform adaptive limiter so it reduces refill rate
                try:
                    await REST_RATE.on_rate_limit(retry_after)
                except Exception:
                    pass
                await asyncio.sleep(retry_after)
            else:
                logging.warning(f"HTTP error attempt {attempt+1}: {e}")
        except Exception as e:
            logging.warning(f"Error on attempt {attempt+1}: {e}")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 10)
    logging.error("Max retries reached for request.")
    return None


# ---------------------------
# Cloning functions (optimized)
# ---------------------------
async def copy_guild_info(source: discord.Guild, target: discord.Guild):
    logging.info("Copying guild info.")
    icon = await fetch_image(source.icon.url if source.icon else None)
    banner = await fetch_image(source.banner.url if source.banner else None)
    # small jitter
    await asyncio.sleep(random.uniform(0, 0.06))
    await safe_request(target.edit, name=source.name, icon=icon, banner=banner)
    logging.info("Guild info copied.")


async def create_channel_worker(target_guild: discord.Guild, category,
                                source_channel):
    try:
        await asyncio.sleep(random.uniform(0, 0.08))
        if isinstance(source_channel, discord.TextChannel):
            new_channel = await safe_request(
                target_guild.create_text_channel,
                name=source_channel.name,
                category=category,
                topic=source_channel.topic,
                nsfw=source_channel.nsfw,
                slowmode_delay=source_channel.slowmode_delay,
                position=source_channel.position)
            if new_channel:
                logging.info(f"Created text channel {source_channel.name}")
        elif isinstance(source_channel, discord.VoiceChannel):
            new_channel = await safe_request(
                target_guild.create_voice_channel,
                name=source_channel.name,
                category=category,
                bitrate=source_channel.bitrate,
                user_limit=source_channel.user_limit,
                position=source_channel.position)
            if new_channel:
                logging.info(f"Created voice channel {source_channel.name}")
    except Exception as e:
        logging.debug(
            f"Error creating channel {getattr(source_channel, 'name', 'unknown')}: {e}"
        )


async def copy_channels(source: discord.Guild, target: discord.Guild):
    logging.info("Copying channels.")
    existing_categories = {c.name for c in target.categories}
    category_tasks = []
    for category in source.categories:
        if category.name in existing_categories:
            continue
        category_tasks.append(
            safe_request(target.create_category, name=category.name))
    created_categories = await asyncio.gather(*category_tasks,
                                              return_exceptions=True)

    # map names to created category objects (best-effort)
    name_to_category = {}
    created_iter = iter(created_categories)
    for category in source.categories:
        if category.name in existing_categories:
            name_to_category[category.name] = discord.utils.get(
                target.categories, name=category.name)
        else:
            try:
                created = next(created_iter)
            except StopIteration:
                created = None
            if isinstance(created, discord.CategoryChannel):
                name_to_category[category.name] = created
            else:
                name_to_category[category.name] = discord.utils.get(
                    target.categories, name=category.name)

    channel_tasks = []
    for category in source.categories:
        target_category = name_to_category.get(category.name)
        for channel in category.channels:
            channel_tasks.append(
                create_channel_worker(target, target_category, channel))

    async def run_bounded(tasks, max_concurrent):
        semaphore = asyncio.Semaphore(max_concurrent)

        async def worker(task_coro):
            async with semaphore:
                return await task_coro

        wrapped = [worker(t) for t in tasks]
        return await asyncio.gather(*wrapped, return_exceptions=True)

    await run_bounded(channel_tasks, MAX_CLONE_CHANNEL_TASKS)
    logging.info("Channels copied.")


async def create_role_worker(target_guild: discord.Guild, role: discord.Role):
    await asyncio.sleep(0.10 + random.uniform(0, 0.06))
    try:
        new_role = await safe_request(target_guild.create_role,
                                      name=role.name,
                                      permissions=role.permissions,
                                      colour=role.colour,
                                      hoist=role.hoist,
                                      mentionable=role.mentionable)
        if new_role:
            logging.info(f"Created role {role.name}")
            return new_role
    except Exception as e:
        logging.debug(f"Failed to create role {role.name}: {e}")
    return None


async def copy_roles(source: discord.Guild, target: discord.Guild):
    logging.info("Copying roles.")
    existing = {r.name for r in target.roles}
    tasks = []
    for role in source.roles:
        if role.name == "@everyone" or role.name in existing:
            continue
        tasks.append(create_role_worker(target, role))
    semaphore = asyncio.Semaphore(MAX_CLONE_ROLE_TASKS)

    async def worker(task_coro):
        async with semaphore:
            return await task_coro

    wrapped = [worker(t) for t in tasks]
    await asyncio.gather(*wrapped, return_exceptions=True)
    logging.info("Roles copied.")


async def create_emoji_worker(target_guild: discord.Guild,
                              emoji: discord.Emoji):
    await asyncio.sleep(0.18 + random.uniform(0, 0.04))
    try:
        image = await fetch_image(emoji.url)
        if not image:
            return None
        image_file = io.BytesIO(image)
        new_emoji = await safe_request(target_guild.create_custom_emoji,
                                       name=emoji.name,
                                       image=image_file.read())
        if new_emoji:
            logging.info(f"Created emoji {emoji.name}")
            return new_emoji
    except Exception as e:
        logging.debug(f"Failed to create emoji {emoji.name}: {e}")
    return None


async def copy_emojis(source: discord.Guild, target: discord.Guild):
    logging.info("Copying emojis.")
    existing = {e.name for e in target.emojis}
    tasks = []
    for emoji in source.emojis:
        if emoji.name in existing:
            continue
        tasks.append(create_emoji_worker(target, emoji))
    semaphore = asyncio.Semaphore(MAX_CLONE_EMOJI_TASKS)

    async def worker(task_coro):
        async with semaphore:
            return await task_coro

    wrapped = [worker(t) for t in tasks]
    await asyncio.gather(*wrapped, return_exceptions=True)
    logging.info("Emojis copied.")


async def create_webhook_worker(target_guild: discord.Guild, channel_name: str,
                                webhook: discord.Webhook):
    await asyncio.sleep(0.12 + random.uniform(0, 0.05))
    try:
        target_channel = discord.utils.get(target_guild.channels,
                                           name=channel_name)
        if target_channel and isinstance(target_channel, discord.TextChannel):
            avatar = await fetch_image(
                webhook.avatar.url if webhook.avatar else None)
            new_wh = await safe_request(target_channel.create_webhook,
                                        name=webhook.name,
                                        avatar=avatar)
            if new_wh:
                logging.info(
                    f"Created webhook {webhook.name} in {channel_name}")
                return new_wh
    except Exception as e:
        logging.debug(
            f"Failed to create webhook {getattr(webhook, 'name', 'unknown')}: {e}"
        )
    return None


async def copy_webhooks(source: discord.Guild, target: discord.Guild):
    logging.info("Copying webhooks.")
    tasks = []
    for channel in source.channels:
        try:
            if not isinstance(channel,
                              (discord.TextChannel, discord.abc.GuildChannel)):
                continue
            webhooks = await channel.webhooks()
            for wh in webhooks:
                tasks.append(create_webhook_worker(target, channel.name, wh))
        except Exception as e:
            logging.debug(
                f"Could not fetch webhooks from {getattr(channel, 'name', 'unknown')}: {e}"
            )
    semaphore = asyncio.Semaphore(MAX_CLONE_WEBHOOK_TASKS)

    async def worker(task_coro):
        async with semaphore:
            return await task_coro

    wrapped = [worker(t) for t in tasks]
    await asyncio.gather(*wrapped, return_exceptions=True)
    logging.info("Webhooks copied.")


async def clone_server(source: discord.Guild, target: discord.Guild):
    # permission checks: ensure we have Manage Guild on target
    try:
        me = target.get_member(client.user.id) or await target.fetch_member(
            client.user.id)
        if not (me.guild_permissions.manage_guild
                or me.guild_permissions.administrator):
            logging.warning(
                "Bot lacks manage_guild/admin permission on target. Aborting clone."
            )
            return False
    except Exception as e:
        logging.warning(f"Permission check failed: {e}")
        return False

    # Correct order: guild info, channels, emojis+webhooks, roles last
    await copy_guild_info(source, target)  # Step 1: pfp + name
    await copy_channels(source, target)  # Step 2: channels
    await asyncio.gather(copy_emojis(source, target),
                         copy_webhooks(source,
                                       target))  # Step 3: emojis + webhooks
    await copy_roles(source, target)  # Step 4: roles (last)

    logging.info("Clone complete.")
    return True


# ---------------------------
# Spam / copy messages optimized (using send queue)
# ---------------------------
async def get_or_create_webhook(
        channel: discord.TextChannel) -> Optional[discord.Webhook]:
    if channel.id in _cached_webhooks:
        wbs = _cached_webhooks[channel.id]
        if wbs:
            return wbs[0]
    try:
        webhooks = await channel.webhooks()
        if webhooks:
            _cached_webhooks[channel.id] = webhooks
            return webhooks[0]
        wh = await channel.create_webhook(name="Message Copier")
        _cached_webhooks[channel.id] = [wh]
        return wh
    except Exception as e:
        logging.debug(f"Failed to get/create webhook in {channel.id}: {e}")
        return None


async def copy_messages_cmd(source_channel_id: int,
                            destination_channel_id: int,
                            limit: int = 100):
    async with copy_semaphore:
        source_channel = client.get_channel(source_channel_id)
        destination_channel = client.get_channel(destination_channel_id)
        if source_channel is None or destination_channel is None:
            return False, "Could not find one or both channels."

        messages = []
        async for msg in source_channel.history(limit=limit,
                                                oldest_first=True):
            messages.append(msg)

        wh = None
        if isinstance(destination_channel, discord.TextChannel):
            wh = await get_or_create_webhook(destination_channel)
        if wh is None:
            return False, "Failed to obtain or create a webhook in destination."

        # Prefetch attachments for the batch to avoid re-downloading during send
        batch = []
        for idx, msg in enumerate(messages, start=1):
            batch.append(msg)
            if len(batch) >= MESSAGE_BATCH_SIZE or idx == len(messages):
                async with webhook_send_semaphore:
                    for m in batch:
                        files = []
                        for attachment in m.attachments:
                            try:
                                b = await attachment.read()
                                files.append(
                                    discord.File(io.BytesIO(b),
                                                 filename=attachment.filename))
                            except Exception:
                                pass
                        try:
                            await enqueue_send(wh.send,
                                               content=m.content or "\u200b",
                                               username=m.author.name,
                                               avatar_url=m.author.avatar.url
                                               if m.author.avatar else None,
                                               files=files,
                                               wait_result=True)
                        except Exception as e:
                            logging.debug(f"Webhook send error queued: {e}")
                await asyncio.sleep(MESSAGE_BATCH_PAUSE)
                batch = []
        return True, f"Copied {len(messages)} messages."


# ---------------------------
# Commands (public) - use enqueue_send where needed
# ---------------------------
@client.command()
async def selfreact(ctx, emoji: str = None):
    global self_react_enabled, self_react_emoji
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await asyncio.sleep(0.1)  # ensure deletion first
    if emoji:
        self_react_enabled = True
        self_react_emoji = emoji
        await boxed_temp_msg(ctx, f"✅ Self-reaction enabled with {emoji}")
    else:
        self_react_enabled = False
        await boxed_temp_msg(ctx, "❌ Self-reaction disabled")


@client.command()
async def react(ctx, member: discord.Member = None, emoji: str = None):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    if not member and not emoji:
        react_targets.clear()
        await boxed_temp_msg(ctx, "❌ Reactions to all users disabled")
        return
    uid = member.id
    if uid in react_targets and react_targets[uid] == emoji:
        del react_targets[uid]
        await boxed_temp_msg(ctx, f"❌ Reaction removed for {member.name}")
    else:
        react_targets[uid] = emoji
        await boxed_temp_msg(ctx,
                             f"✅ Now reacting to {member.name} with {emoji}")


@client.command()
async def reactlist(ctx):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    if not react_targets:
        await enqueue_send(ctx.send, "```📛 No active reaction targets.```")
        return
    msg = "╔═══════📦 Reaction Targets 📦═══════╗\n"
    for uid, emoji in react_targets.items():
        try:
            user = client.get_user(uid) or await client.fetch_user(uid)
            msg += f"║ {user.name}#{user.discriminator} → {emoji}\n"
        except Exception:
            msg += f"║ Unknown User (ID: {uid}) → {emoji}\n"
    msg += "╚══════════════════════════════════╝"
    await enqueue_send(ctx.send, f"```{msg}```")


@client.command()
async def ap(ctx, member: discord.Member):
    global ap_running
    ap_running = True
    try:
        await ctx.message.delete()
    except Exception:
        pass
    client.loop.create_task(
        boxed_temp_msg(ctx, f"⚙️ Auto pressure started on {member.name}"))
    try:
        with open("pack.txt", "r", encoding="utf-8") as f:
            content = f.read()
            # Each '---' block = one pack (line breaks inside are preserved)
            groups = [
                grp.strip() for grp in content.split("---") if grp.strip()
            ]
    except FileNotFoundError:
        await boxed_temp_msg(ctx, "❌ pack.txt not found.")
        return

    def split_chunks(text: str, max_len: int):
        return [text[i:i + max_len] for i in range(0, len(text), max_len)]

    mention_suffix = f" {member.mention}"
    max_content_len = 2000 - len(mention_suffix)

    async def ap_worker():
        nonlocal groups, member
        while ap_running:
            for group in groups:
                if not ap_running:
                    break
                # Only chunk if the block is too long
                chunks = split_chunks(group, max_content_len)
                for chunk in chunks:
                    if not ap_running:
                        break
                    try:
                        await enqueue_send(ctx.send,
                                           f"{chunk}{mention_suffix}")
                    except Exception:
                        pass
                    await asyncio.sleep(max(ap_delay, 0.01))
            await asyncio.sleep(0.01)

    client.loop.create_task(ap_worker())


@client.command()
async def apstop(ctx):
    global ap_running
    try:
        await ctx.message.delete()
    except Exception:
        pass
    ap_running = False
    await boxed_temp_msg(ctx, "🛑 Auto pressure stopped")


@client.command()
async def apspeed(ctx, delay: float):
    global ap_delay
    try:
        await ctx.message.delete()
    except Exception:
        pass
    ap_delay = delay
    await boxed_temp_msg(ctx, f"⏱️ AP speed set to {delay} seconds")


@client.command()
async def ladder(ctx):
    global ladder_running
    ladder_running = True
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await boxed_temp_msg(ctx, "📡 Ladder started")
    try:
        with open("reply.txt", "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except FileNotFoundError:
        await boxed_temp_msg(ctx, "❌ reply.txt not found.")
        return

    async def ladder_worker():
        nonlocal lines
        while ladder_running:
            for line in lines:
                if not ladder_running:
                    break
                try:
                    await enqueue_send(ctx.send, line)
                except Exception:
                    pass
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)

    client.loop.create_task(ladder_worker())


@client.command()
async def ladderstop(ctx):
    global ladder_running
    try:
        await ctx.message.delete()
    except Exception:
        pass
    ladder_running = False
    await boxed_temp_msg(ctx, "🛑 Ladder stopped")


@client.command()
async def pfp(ctx, member: discord.Member = None):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    member = member or ctx.author
    try:
        await enqueue_send(
            ctx.send,
            f"🖼️ **{member.name}'s Profile Picture:**\n{member.avatar.url}")
    except Exception as e:
        await boxed_temp_msg(ctx, f"❌ Failed to fetch pfp: {e}")


@client.command()
async def c(ctx, amount: int):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    deleted = 0
    try:
        async for msg in ctx.channel.history(limit=amount):
            try:
                await enqueue_send(msg.delete, wait_result=True)
                deleted += 1
            except Exception:
                continue
    except Exception:
        pass
    await boxed_temp_msg(ctx, f"🧹 Deleted {deleted} messages")


@client.command()
async def calculator(ctx, *, question):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    try:
        answer = eval(question, {"__builtins__": {}}, {})
        await enqueue_send(ctx.send, f"**# 🧠 Answer:** `{answer}`")
    except Exception:
        await enqueue_send(ctx.send, "**# Invalid equation.**")


@client.command()
async def coinflip(ctx):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    await enqueue_send(ctx.send,
                       f"**# {random.choice(['🪙 Heads', '🪙 Tails'])}**")


@client.command()
async def eightball(ctx):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    responses = [
        "Yes", "No", "Maybe", "Definitely", "Absolutely not",
        "Try again later", "I can't tell you now", "Without a doubt"
    ]
    await enqueue_send(ctx.send, f"**# 🎱 {random.choice(responses)}**")


@client.command()
async def gay(ctx, member: discord.Member = None):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    percent = random.randint(0, 100)
    target = member.mention if member else ctx.author.mention
    await enqueue_send(ctx.send, f"**# 🌈 {target} is {percent}% gay!**")


@client.command()
async def autobold(ctx):
    global autobold_enabled
    try:
        await ctx.message.delete()
    except Exception:
        pass
    autobold_enabled = not autobold_enabled
    status = "enabled ✅" if autobold_enabled else "disabled ❌"
    await boxed_temp_msg(ctx, f"**Autobold is now {status}**")


@client.command()
async def mimic(ctx, member: discord.Member = None):
    global mimic_enabled, mimic_target_id
    try:
        await ctx.message.delete()
    except Exception:
        pass
    if member:
        mimic_enabled = True
        mimic_target_id = member.id
        await boxed_temp_msg(ctx, f"🌀 Mimic started for {member.name}")
    else:
        mimic_enabled = False
        mimic_target_id = None
        await boxed_temp_msg(ctx, "🛑 Mimic stopped")


@client.command(name="translator")
async def toggle_translator(ctx, lang=None):
    global translator_enabled, translator_target_lang
    try:
        await ctx.message.delete()
    except Exception:
        pass
    if lang:
        translator_enabled = True
        translator_target_lang = lang
        client.loop.create_task(
            boxed_temp_msg(ctx, f"🌐 Translator enabled → `{lang}`"))
    else:
        await boxed_temp_msg(
            ctx, "❌ Please provide a language code (e.g. `ja`, `fr`, `es`)")


@client.command()
async def translatoroff(ctx):
    global translator_enabled
    try:
        await ctx.message.delete()
    except Exception:
        pass
    translator_enabled = False
    client.loop.create_task(boxed_temp_msg(ctx, "🛑 Translator disabled"))


@client.command()
async def copy(ctx, source_guild_id: int, target_guild_id: int):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    target_guild = client.get_guild(target_guild_id)
    source_guild = client.get_guild(source_guild_id)

    if not source_guild:
        await boxed_temp_msg(ctx,
                             f"Source guild ID {source_guild_id} not found.")
        return

    if not target_guild:
        await boxed_temp_msg(ctx,
                             f"Target guild ID {target_guild_id} not found.")
        return

    await boxed_temp_msg(
        ctx,
        f"Cloning roles & channels from '{source_guild.name}' to '{target_guild.name}'..."
    )
    ok = await clone_server(source_guild, target_guild)
    if ok:
        await boxed_temp_msg(ctx, "Server structure cloned successfully!")
    else:
        await boxed_temp_msg(ctx, "Clone failed (check permissions/logs).")


@client.command()
async def copy_messages(ctx,
                        source_channel_id: int,
                        destination_channel_id: int,
                        limit: int = 100):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    success, msg = await copy_messages_cmd(source_channel_id,
                                           destination_channel_id, limit)
    if success:
        await boxed_temp_msg(ctx, msg)
    else:
        await boxed_temp_msg(ctx, f"Error: {msg}")


@client.command()
async def stats(ctx):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    uptime = time.time() - client.start_time
    uptime_str = str(datetime.timedelta(seconds=int(uptime)))
    ws_ping = client.latency * 1000
    # measure message send latency briefly via queue
    start_time = time.time()
    test_msg = await enqueue_send(ctx.send, "Testing message send time...")
    response_time = time.time() - start_time
    start_time = time.time()
    await enqueue_send(test_msg.edit, "Edited!", wait_result=True)
    edit_time = time.time() - start_time
    stats_message = (f"🛰️ WebSocket Ping: {ws_ping:.2f}ms\n"
                     f"⚡ Message Send Time: {response_time * 1000:.2f}ms\n"
                     f"✏️ Edit Delay: {edit_time * 1000:.2f}ms\n"
                     f"⏳ Uptime: {uptime_str}")
    await enqueue_send(ctx.send, f"```{stats_message}```")
    try:
        await enqueue_send(test_msg.delete, wait_result=True)
    except Exception:
        pass


@client.command(name="help")
async def custom_help(ctx):
    try:
        await ctx.message.delete()
    except Exception:
        pass
    ascii_art = """
  ⣿⠲⠤⣀⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⣸⡏⠀⠀⠀⠉⠳⢄⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⣿⠀⠀⠀⠀⠀⠀⠀⠉⠲⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⢰⡏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠲⣄⠀⠀⠀⡰⠋⢙⣿⣦⡀⠀⠀⠀⠀⠀
⠸⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣙⣦⣮⣤⡀⣸⣿⣿⣿⣆⠀⠀⠀⠀
⠀⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣼⣿⣿⣿⣿⠀⣿⢟⣫⠟⠋⠀⠀⠀⠀
⠀⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣷⣷⣿⡁⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⢸⣿⣿⣧⣿⣿⣆⠙⢆⡀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢾⣿⣤⣿⣿⣿⡟⠹⣿⣿⣿⣿⣷⡀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⣿⣧⣴⣿⣿⣿⣿⠏⢧⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣼⢻⣿⣿⣿⣿⣿⣿⣿⣿⡟⠀⠈⢳⡀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⡏⣸⣿⣿⣿⣿⣿⣿⣿⣿⠃⠀⠀⠀⢳⡀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣸⢀⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡇⠸⣿⣿⣿⣿⣿⣿⣿⣿⠏⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡇⠀⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⡇⢠⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⠁⢸⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣼⢸⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣾⣿⢸⣿⣿⣿⣿⣿⣿⣿⣿⡄⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣸⣿⣿⣾⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣇⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠛⠻⠿⣿⣿⣿⡿⠿⠿⠿⠿⠿⠿⢿⣿⣿⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀    
"""
    help_text = (
        f"```ini\n"
        f"{ascii_art}"
        f"[📖 Selfbot Help Menu]\n\n"
        f"[📚 Core Tools]\n"
        f".selfreact <emoji> - Start/stop self-reactions\n"
        f".react @user <emoji> - Start/stop reactions\n"
        f".react - Disable all reactions\n"
        f".reactlist - Show reaction targets\n"
        f".ap @user - Start auto pressure\n"
        f".apstop - Stop auto pressure\n"
        f".apspeed <delay> - Set auto pressure delay\n"
        f".ladder - Start ladder spam\n"
        f".ladderstop - Stop ladder\n"
        f".copy <source_guild_id> <target_guild_id> - Clone server structure\n"
        f".copy_messages <source_channel_id> <dest_channel_id> <limit> - Copy messages\n"
        f"[🎮 Fun Features]\n"
        f".calculator <expr> - Solve math\n"
        f".coinflip - Flip a coin\n"
        f".eightball - Magic 8-ball\n"
        f".gay @user - Gay meter\n"
        f".mimic - Toggle mimic mode\n"
        f".translator <lang> - Enable auto translation\n"
        f".translatoroff - Disable auto translation\n"
        f".stats - Show ping/send time/uptime\n\n"
        f"Made by .. 🌑\n"
        f"```")
    msg = await enqueue_send(ctx.send, help_text)
    await asyncio.sleep(30)
    try:
        await enqueue_send(msg.delete, wait_result=True)
    except Exception:
        pass


# ---------------------------
# Events / background handlers
# ---------------------------
@client.event
async def on_ready():
    global aio_session
    if aio_session is None:
        aio_session = aiohttp.ClientSession()
    client.start_time = time.time()
    logging.info(f"✅ Logged in as {client.user}")
    # start background workers
    client.loop.create_task(mimic_handler_background())
    client.loop.create_task(send_worker())
    for _ in range(3):
        client.loop.create_task(react_worker())


async def mimic_handler_background():
    while True:
        if mimic_enabled and mimic_queue:
            msg, channel = mimic_queue.popleft()
            try:
                await enqueue_send(msg.reply, format_mimic(msg.content))
            except Exception as e:
                logging.debug(f"[Mimic Error] {e}")
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.01)


@client.event
async def on_message(message):
    # Process self reactions & react targets quickly using queue
    try:
        # Self reactions
        if message.author.id == client.user.id and self_react_enabled and self_react_emoji:
            try:
                # fire-and-forget enqueue of reaction
                client.loop.create_task(
                    enqueue_react(message.add_reaction, self_react_emoji))
            except Exception:
                pass

        # Targeted reactions
        if message.author.id in react_targets:
            try:
                client.loop.create_task(
                    enqueue_react(message.add_reaction,
                                  react_targets[message.author.id]))
            except Exception:
                pass

        # Mimic queue
        if (mimic_enabled and mimic_target_id
                and message.author.id == mimic_target_id
                and not getattr(message.author, "bot", False)
                and message.content.strip()):
            mimic_queue.append((message, message.channel))

        # Translator (offloaded to threadpool to avoid blocking)
        if message.author.id == client.user.id and translator_enabled and message.content.strip(
        ):

            async def do_translate_and_edit(m):
                loop = asyncio.get_running_loop()
                try:
                    translated = await loop.run_in_executor(
                        thread_pool, lambda: translator.translate(
                            m.content, dest=translator_target_lang))
                    text = getattr(translated, "text", str(translated))
                    await enqueue_send(m.edit, text, wait_result=True)
                except Exception as e:
                    logging.debug(
                        f"[Translator Error] Failed to translate → {e}")

            client.loop.create_task(do_translate_and_edit(message))

        # Autobold
        if message.author.id == client.user.id and autobold_enabled and not message.content.startswith(
                "#"):
            try:
                client.loop.create_task(
                    enqueue_send(message.edit,
                                 f"# {message.content}",
                                 wait_result=False))
            except Exception:
                pass
    except Exception:
        pass

    await client.process_commands(message)


@client.event
async def on_socket_response(payload):
    # Fast reaction using low-level http call (queued)
    if payload.get("t") != "MESSAGE_CREATE":
        return
    try:
        data = payload["d"]
        author_id = int(data["author"]["id"])
        channel_id = int(data["channel_id"])
        message_id = int(data["id"])
    except Exception:
        return

    if author_id == client.user.id and self_react_enabled and self_react_emoji:
        # queue http add reaction (wrapped async)
        async def http_add(channel_id, message_id, emoji):
            try:
                client.http.add_reaction(channel_id, message_id, emoji)
            except Exception:
                pass

        client.loop.create_task(
            enqueue_react(http_add, channel_id, message_id, self_react_emoji))

    if author_id in react_targets:
        emoji = react_targets[author_id]

        async def http_add2(channel_id, message_id, emoji2):
            try:
                client.http.add_reaction(channel_id, message_id, emoji2)
            except Exception:
                pass

        client.loop.create_task(
            enqueue_react(http_add2, channel_id, message_id, emoji))


# ---------------------------
# Shutdown helpers
# ---------------------------
async def graceful_close():
    global aio_session, thread_pool
    try:
        if aio_session:
            await aio_session.close()
    except Exception:
        pass
    try:
        thread_pool.shutdown(wait=False)
    except Exception:
        pass


# ---------------------------
# Run bot
# ---------------------------
try:
    client.run(TOKEN)
except KeyboardInterrupt:
    logging.info("Shutting down...")
    try:
        asyncio.run(graceful_close())
    except Exception:
        pass
except Exception as e:
    logging.error(f"An error occurred while running the bot: {e}")
    try:
        asyncio.run(graceful_close())
    except Exception:
        pass

