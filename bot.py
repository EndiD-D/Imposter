import os
import random
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import discord
from discord.ext import commands
from dotenv import load_dotenv

from flask import Flask
from threading import Thread

app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

@app.get("/health")
def health():
    return "healthy", 200

def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))

Thread(target=run_web, daemon=True).start()
# =========================
# ENV
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN. Put it in .env as DISCORD_TOKEN=...")


# =========================
# CONFIG
# =========================
PREFIX = "!"
WORDS_FILE = "words.txt"

MIN_PLAYERS = 3
MAX_CLUE_LEN = 80

TURN_TIMEOUT = 75
BETWEEN_TURNS = 0.6

# Voting happens after this many rounds, then the game ENDS with reveal
ROUNDS_BEFORE_FINAL_VOTE = 3
VOTE_TIMEOUT = 60

ALLOW_2_IMPOSTERS_AT = 7  # 7+ players => host can pick 1 or 2 imposters


# =========================
# WORDS
# =========================
def load_words() -> List[str]:
    words: List[str] = []
    if os.path.exists(WORDS_FILE):
        with open(WORDS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                w = line.strip()
                if not w or w.startswith("#"):
                    continue
                if " " in w:
                    continue
                words.append(w.upper())

    if not words:
        words = ["PIZZA", "AIRPLANE", "VOLCANO", "BICYCLE", "CHOCOLATE", "PYRAMID", "ROBOT", "CASTLE"]

    seen = set()
    out = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


WORDS = load_words()


# =========================
# STATE
# =========================
@dataclass
class Player:
    user_id: int
    alive: bool = True  # kept for potential future, but no ejections now


@dataclass
class GameState:
    guild_id: int
    channel_id: int
    host_id: int

    started: bool = False

    # players dict + join order list (order must NOT change)
    players: Dict[int, Player] = field(default_factory=dict)
    join_order: List[int] = field(default_factory=list)

    secret_word: Optional[str] = None
    imposters: Set[int] = field(default_factory=set)

    # lobby msg for buttons
    lobby_message_id: Optional[int] = None

    # round/turn
    round_no: int = 0
    current_turn_index: int = 0
    current_round_clues: Dict[int, str] = field(default_factory=dict)
    history: List[Tuple[int, Dict[int, str]]] = field(default_factory=list)

    # turn tracking for interactions
    expecting_clue_from: Optional[int] = None

    # voting (final)
    voting_open: bool = False
    votes: Dict[int, int] = field(default_factory=dict)  # voter_id -> target_id (0=skip)
    vote_task: Optional[asyncio.Task] = None

    # loop task
    game_task: Optional[asyncio.Task] = None

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_host(self, uid: int) -> bool:
        return uid == self.host_id

    def alive_players(self) -> List[int]:
        # fixed order: join_order filtered by alive
        return [uid for uid in self.join_order if uid in self.players and self.players[uid].alive]


GAMES: Dict[Tuple[int, int], GameState] = {}


# =========================
# BOT
# =========================
intents = discord.Intents.all()


bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


# =========================
# HELPERS
# =========================
def e(title: str, desc: str = "", color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color)


def mention(guild: discord.Guild, uid: int) -> str:
    m = guild.get_member(uid)
    return m.mention if m else f"<@{uid}>"


def name(guild: discord.Guild, uid: int) -> str:
    m = guild.get_member(uid)
    return m.display_name if m else str(uid)


def fmt_list(guild: discord.Guild, ids: List[int]) -> str:
    return ", ".join(mention(guild, i) for i in ids) if ids else "—"


def imposter_options(n: int) -> List[int]:
    return [1, 2] if n >= ALLOW_2_IMPOSTERS_AT else [1]


def get_state_from_channel(guild_id: int, channel_id: int) -> Optional[GameState]:
    return GAMES.get((guild_id, channel_id))


# =========================
# LOBBY VIEW (JOIN/LEAVE)
# =========================
class LobbyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="➕", custom_id="imposter:lobby_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Server channel only.", ephemeral=True)

        state = get_state_from_channel(interaction.guild.id, interaction.channel.id)
        if not state:
            return await interaction.response.send_message("No lobby here. Host should use `!startgame` to create one.", ephemeral=True)

        async with state.lock:
            if state.started:
                return await interaction.response.send_message("Game already started.", ephemeral=True)
            if interaction.user.id in state.players:
                return await interaction.response.send_message("You’re already in the lobby.", ephemeral=True)

            state.players[interaction.user.id] = Player(user_id=interaction.user.id)
            state.join_order.append(interaction.user.id)

        await refresh_lobby_embed(interaction.guild, interaction.channel, state)
        await interaction.response.send_message("✅ Joined the lobby!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.danger, emoji="➖", custom_id="imposter:lobby_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Server channel only.", ephemeral=True)

        state = get_state_from_channel(interaction.guild.id, interaction.channel.id)
        if not state:
            return await interaction.response.send_message("No lobby here.", ephemeral=True)

        async with state.lock:
            if state.started:
                return await interaction.response.send_message("Can’t leave mid-game. Ask host to `!endgame`.", ephemeral=True)
            if interaction.user.id not in state.players:
                return await interaction.response.send_message("You’re not in this lobby.", ephemeral=True)

            # host leaving closes lobby
            if interaction.user.id == state.host_id:
                GAMES.pop((interaction.guild.id, interaction.channel.id), None)
                return await interaction.response.send_message("🧹 Host left — lobby closed.", ephemeral=True)

            # remove player
            del state.players[interaction.user.id]
            state.join_order = [uid for uid in state.join_order if uid != interaction.user.id]

        await refresh_lobby_embed(interaction.guild, interaction.channel, state)
        await interaction.response.send_message("✅ Left the lobby.", ephemeral=True)


async def refresh_lobby_embed(guild: discord.Guild, channel: discord.TextChannel, state: GameState):
    if not state.lobby_message_id:
        return
    try:
        msg = await channel.fetch_message(state.lobby_message_id)
    except discord.NotFound:
        state.lobby_message_id = None
        return

    ids = list(state.players.keys())
    ordered = state.join_order[:]  # already in correct fixed order

    emb = e("🎭 Imposter — Lobby", "Click **Join** to enter. When ready, host runs **!startgame** again to begin.")
    emb.add_field(name="Host", value=mention(guild, state.host_id), inline=True)
    emb.add_field(name="Players", value=f"{len(ids)}", inline=True)
    emb.add_field(name="Fixed Order", value=fmt_list(guild, ordered), inline=False)
    emb.set_footer(text="No DMs. Roles are revealed privately (ephemeral) after start.")
    await msg.edit(embed=emb, view=LobbyView())


# =========================
# REVEAL ROLE VIEW (EPHEMERAL)
# =========================
class RevealRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Reveal Role", style=discord.ButtonStyle.primary, emoji="🎭")
    async def reveal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Server channel only.", ephemeral=True)

        state = get_state_from_channel(interaction.guild.id, interaction.channel.id)
        if not state or not state.started:
            return await interaction.response.send_message("No active game here.", ephemeral=True)

        uid = interaction.user.id
        async with state.lock:
            if uid not in state.players:
                return await interaction.response.send_message("You’re not in this game.", ephemeral=True)

            if uid in state.imposters:
                emb = e("🕵️ You are the IMPOSTER", "Blend in. Don’t get caught.", discord.Color.red())
                emb.add_field(name="Secret Word", value="❌ You don’t know it.", inline=False)
            else:
                emb = e("✅ You are a CIVILIAN", "Give clues that hint the word without saying it.", discord.Color.green())
                emb.add_field(name="Secret Word", value=f"**{state.secret_word}**", inline=False)

        await interaction.response.send_message(embed=emb, ephemeral=True)


# =========================
# TURN CLUE MODAL + VIEW
# =========================
class TurnClueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=TURN_TIMEOUT + 10)

    @discord.ui.button(label="Submit Clue", style=discord.ButtonStyle.success, emoji="✍️")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Server channel only.", ephemeral=True)

        state = get_state_from_channel(interaction.guild.id, interaction.channel.id)
        if not state or not state.started:
            return await interaction.response.send_message("No active game here.", ephemeral=True)

        uid = interaction.user.id

        async with state.lock:
            if state.voting_open:
                return await interaction.response.send_message("Voting is open — no clues right now.", ephemeral=True)

            if uid not in state.players:
                return await interaction.response.send_message("You’re not in this game.", ephemeral=True)

            if state.expecting_clue_from != uid:
                who = mention(interaction.guild, state.expecting_clue_from) if state.expecting_clue_from else "—"
                return await interaction.response.send_message(f"Not your turn. Current turn: {who}", ephemeral=True)

            if uid in state.current_round_clues:
                return await interaction.response.send_message("You already submitted this round.", ephemeral=True)

        await interaction.response.send_modal(ClueModal())


class ClueModal(discord.ui.Modal, title="Submit your clue"):
    clue = discord.ui.TextInput(
        label="Clue (short, no exact word)",
        placeholder="Example: 'warm', 'crispy', 'delivery'...",
        max_length=MAX_CLUE_LEN,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("Server channel only.", ephemeral=True)

        state = get_state_from_channel(interaction.guild.id, interaction.channel.id)
        if not state or not state.started:
            return await interaction.response.send_message("No active game here.", ephemeral=True)

        uid = interaction.user.id
        clue_text = str(self.clue.value).strip()
        if not clue_text:
            return await interaction.response.send_message("Clue can’t be empty.", ephemeral=True)

        async with state.lock:
            if state.voting_open:
                return await interaction.response.send_message("Voting is open — no clues right now.", ephemeral=True)

            if state.expecting_clue_from != uid:
                who = mention(interaction.guild, state.expecting_clue_from) if state.expecting_clue_from else "—"
                return await interaction.response.send_message(f"Not your turn. Current turn: {who}", ephemeral=True)

            # block exact word
            if state.secret_word and clue_text.upper() == state.secret_word.upper():
                return await interaction.response.send_message("Don’t type the exact secret word.", ephemeral=True)

            state.current_round_clues[uid] = clue_text

        # Publicly show: "Endi: nice smell"
        await interaction.channel.send(f"**{name(interaction.guild, uid)}:** {clue_text}")
        await interaction.response.send_message("✅ Clue submitted!", ephemeral=True)


# =========================
# VOTING (FINAL, NO EJECTION)
# =========================
class VoteView(discord.ui.View):
    def __init__(self, guild: discord.Guild, state: GameState):
        super().__init__(timeout=VOTE_TIMEOUT)
        self.guild = guild
        self.state = state
        self.add_item(VoteSelect(guild, state))

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, 0)

    @discord.ui.button(label="Clear Vote", style=discord.ButtonStyle.danger, emoji="🧹")
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        async with self.state.lock:
            self.state.votes.pop(uid, None)
        await interaction.response.send_message("✅ Cleared your vote.", ephemeral=True)

    async def _record(self, interaction: discord.Interaction, target_id: int):
        uid = interaction.user.id
        async with self.state.lock:
            if not self.state.voting_open:
                return await interaction.response.send_message("Voting is closed.", ephemeral=True)
            if uid not in self.state.players:
                return await interaction.response.send_message("Only players can vote.", ephemeral=True)

            if target_id != 0:
                if target_id == uid:
                    return await interaction.response.send_message("You can’t vote yourself.", ephemeral=True)
                if target_id not in self.state.players:
                    return await interaction.response.send_message("That player isn’t in this game.", ephemeral=True)

            self.state.votes[uid] = target_id

        if target_id == 0:
            await interaction.response.send_message("✅ You voted to skip.", ephemeral=True)
        else:
            await interaction.response.send_message(f"✅ Vote recorded for {mention(interaction.guild, target_id)}.", ephemeral=True)


class VoteSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, state: GameState):
        self.guild = guild
        self.state = state

        opts = []
        for pid in state.alive_players():  # fixed order list
            m = guild.get_member(pid)
            label = (m.display_name if m else str(pid))[:100]
            opts.append(discord.SelectOption(label=label, value=str(pid)))

        super().__init__(placeholder="Who is the imposter?", min_values=1, max_values=1, options=opts)

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        uid = interaction.user.id

        async with self.state.lock:
            if not self.state.voting_open:
                return await interaction.response.send_message("Voting is closed.", ephemeral=True)
            if uid not in self.state.players:
                return await interaction.response.send_message("Only players can vote.", ephemeral=True)
            if target_id == uid:
                return await interaction.response.send_message("You can’t vote yourself.", ephemeral=True)

            self.state.votes[uid] = target_id

        await interaction.response.send_message(f"✅ Vote recorded for {mention(interaction.guild, target_id)}.", ephemeral=True)


async def open_final_voting(guild: discord.Guild, channel: discord.TextChannel, state: GameState):
    async with state.lock:
        state.voting_open = True
        state.votes.clear()

    vote_embed = e("🗳 Final Vote!", "Everyone vote who you think the **imposter** is.\n(Your confirmation is private.)", discord.Color.gold())
    vote_embed.add_field(name="Players", value=fmt_list(guild, state.alive_players()), inline=False)
    await channel.send(embed=vote_embed, view=VoteView(guild, state))

    async def finalize():
        try:
            await asyncio.sleep(VOTE_TIMEOUT)
            await finalize_final_voting(guild, channel, state)
        except asyncio.CancelledError:
            return

    async with state.lock:
        if state.vote_task and not state.vote_task.done():
            state.vote_task.cancel()
        state.vote_task = asyncio.create_task(finalize())


async def finalize_final_voting(guild: discord.Guild, channel: discord.TextChannel, state: GameState):
    async with state.lock:
        if not state.voting_open:
            return

        # count votes
        counts: Dict[int, int] = {}
        for voter, target in state.votes.items():
            if voter not in state.players:
                continue
            if target != 0 and target not in state.players:
                continue
            counts[target] = counts.get(target, 0) + 1

        # top voted (for fun display)
        top_target: Optional[int] = None
        if counts:
            # ignore skip when picking top guess if possible
            non_skip = {k: v for k, v in counts.items() if k != 0}
            pool = non_skip if non_skip else counts
            max_votes = max(pool.values())
            top = [pid for pid, c in pool.items() if c == max_votes]
            top_target = top[0] if len(top) == 1 else None

        state.voting_open = False

        word = state.secret_word or "—"
        imps = list(state.imposters)

    # public summary of votes
    summary = []
    if counts:
        for pid, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:12]:
            if pid == 0:
                summary.append(f"⏭️ Skip: **{c}**")
            else:
                summary.append(f"🗳 {mention(guild, pid)}: **{c}**")
    else:
        summary.append("No votes were cast.")

    reveal = e("🎬 Reveal!", f"Secret word was: **{word}**", discord.Color.purple())
    reveal.add_field(name="Imposter(s)", value=fmt_list(guild, imps), inline=False)
    if top_target is not None:
        reveal.add_field(name="Top Vote Guess", value=f"{mention(guild, top_target)}", inline=False)
    else:
        reveal.add_field(name="Top Vote Guess", value="Tie / no clear top guess.", inline=False)
    reveal.add_field(name="Vote Summary", value="\n".join(summary), inline=False)

    await channel.send(embed=reveal)

    # END GAME + LOBBY (clear state)
    await channel.send(embed=e("🧹 Game ended", "Lobby cleared. Use `!startgame` to create a new one.", discord.Color.orange()))
    GAMES.pop((guild.id, channel.id), None)


# =========================
# GAME LOOP (FIXED ORDER)
# =========================
async def game_loop(guild: discord.Guild, channel: discord.TextChannel, state: GameState):
    try:
        while True:
            async with state.lock:
                if state.voting_open:
                    # shouldn't happen during rounds; just wait
                    pass

                state.round_no += 1
                state.current_round_clues = {}
                state.current_turn_index = 0
                state.expecting_clue_from = None

                turn_order = state.alive_players()  # FIXED join order

            await channel.send(embed=e(f"🌀 Round {state.round_no} begins!", f"Order is fixed:\n{fmt_list(guild, turn_order)}"))

            # turns in fixed order
            for pid in turn_order:
                async with state.lock:
                    if state.voting_open:
                        return
                    state.expecting_clue_from = pid

                turn_embed = e("✍️ Submit your clue", f"It’s {mention(guild, pid)}’s turn.\nClick **Submit Clue**.")
                turn_embed.set_footer(text=f"Timeout: {TURN_TIMEOUT}s")
                await channel.send(embed=turn_embed, view=TurnClueView())

                success = await wait_for_player_clue(state, pid, TURN_TIMEOUT)
                if not success:
                    async with state.lock:
                        if pid not in state.current_round_clues:
                            state.current_round_clues[pid] = "… (timed out)"
                    await channel.send(f"**{name(guild, pid)}:** … (timed out)")

                async with state.lock:
                    state.expecting_clue_from = None

                await asyncio.sleep(BETWEEN_TURNS)

            # recap after full round
            async with state.lock:
                round_clues = dict(state.current_round_clues)
                state.history.append((state.round_no, round_clues))

            recap = e(f"📜 Round {state.round_no} Recap", "Clues submitted this round:")
            lines = []
            for pid in turn_order:
                clue = round_clues.get(pid, "—")
                lines.append(f"• **{name(guild, pid)}:** `{clue}`")
            recap.add_field(name="Clues", value="\n".join(lines) if lines else "—", inline=False)
            await channel.send(embed=recap)

            # after N rounds -> FINAL VOTE -> reveal -> end
            if state.round_no >= ROUNDS_BEFORE_FINAL_VOTE:
                await open_final_voting(guild, channel, state)
                return

    except asyncio.CancelledError:
        return


async def wait_for_player_clue(state: GameState, player_id: int, timeout: float) -> bool:
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        async with state.lock:
            if player_id in state.current_round_clues:
                return True
        await asyncio.sleep(0.2)
    return False


# =========================
# COMMANDS
# =========================
@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    h = e("🎭 Imposter — Help")
    h.add_field(name="Commands", value="`!rules` `!help` `!startgame` `!endgame`", inline=False)
    h.add_field(name="Lobby", value="Join using the **Join** button on the lobby message.", inline=False)
    h.add_field(name="Order", value="Order is fixed by join order and never changes.", inline=False)
    h.add_field(name="Roles", value="After game starts, click **Reveal Role** (private/ephemeral).", inline=False)
    h.add_field(name="Flow", value=f"Plays {ROUNDS_BEFORE_FINAL_VOTE} rounds → recap each round → final vote → reveal → ends.", inline=False)
    await ctx.send(embed=h)


@bot.command(name="rules")
async def cmd_rules(ctx: commands.Context):
    r = e("📜 Rules")
    r.add_field(name="Clues", value=f"On your turn, click Submit Clue and type a short clue (max {MAX_CLUE_LEN}).", inline=False)
    r.add_field(name="Order", value="Order is fixed (join order).", inline=False)
    r.add_field(name="Voting", value="After the final round, everyone votes who the imposter is. Then roles + word are revealed.", inline=False)
    await ctx.send(embed=r)


@bot.command(name="endgame")
async def cmd_endgame(ctx: commands.Context):
    if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
        return

    state = get_state_from_channel(ctx.guild.id, ctx.channel.id)
    if not state:
        return await ctx.send(embed=e("❌ No game here", "Nothing to end.", discord.Color.red()))
    if not state.is_host(ctx.author.id):
        return await ctx.send(embed=e("⛔ Host only", f"Host is {mention(ctx.guild, state.host_id)}.", discord.Color.red()))

    async with state.lock:
        if state.vote_task and not state.vote_task.done():
            state.vote_task.cancel()
        if state.game_task and not state.game_task.done():
            state.game_task.cancel()
        GAMES.pop((ctx.guild.id, ctx.channel.id), None)

    await ctx.send(embed=e("🧹 Ended", "Lobby/game cleared.", discord.Color.orange()))


@bot.command(name="startgame")
async def cmd_startgame(ctx: commands.Context):
    if not ctx.guild or not isinstance(ctx.channel, discord.TextChannel):
        return

    key = (ctx.guild.id, ctx.channel.id)
    state = GAMES.get(key)

    # Create lobby if missing
    if not state:
        state = GameState(guild_id=ctx.guild.id, channel_id=ctx.channel.id, host_id=ctx.author.id)
        state.players[ctx.author.id] = Player(user_id=ctx.author.id)
        state.join_order.append(ctx.author.id)
        GAMES[key] = state

        lobby_embed = e("🎭 Imposter — Lobby", "Click **Join** to enter. When ready, host runs **!startgame** again to begin.")
        lobby_embed.add_field(name="Host", value=mention(ctx.guild, state.host_id), inline=True)
        lobby_embed.add_field(name="Players", value="1", inline=True)
        lobby_embed.add_field(name="Fixed Order", value=fmt_list(ctx.guild, state.join_order), inline=False)
        lobby_embed.set_footer(text="No DMs. Roles are revealed privately (ephemeral) after start.")
        lobby_msg = await ctx.channel.send(embed=lobby_embed, view=LobbyView())
        async with state.lock:
            state.lobby_message_id = lobby_msg.id

        return await ctx.send(embed=e("✅ Lobby created", "Players can join with the button. Run `!startgame` again to start.", discord.Color.green()))

    # Must be host to start actual gameplay
    if not state.is_host(ctx.author.id):
        return await ctx.send(embed=e("⛔ Host only", f"Host is {mention(ctx.guild, state.host_id)}.", discord.Color.red()))

    async with state.lock:
        if state.started:
            return await ctx.send(embed=e("⚠️ Already started", "Game is already running.", discord.Color.orange()))
        if len(state.players) < MIN_PLAYERS:
            await refresh_lobby_embed(ctx.guild, ctx.channel, state)
            return await ctx.send(embed=e("❌ Not enough players", f"Need at least {MIN_PLAYERS}.", discord.Color.red()))

    # Choose imposter count
    opts = imposter_options(len(state.players))
    opt_txt = " / ".join(str(x) for x in opts)
    await ctx.send(embed=e("🎲 Choose imposters", f"{ctx.author.mention}, reply with **{opt_txt}** within 20 seconds."))

    def chk(m: discord.Message) -> bool:
        return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id and m.content.strip().isdigit()

    imp_count = opts[0]
    try:
        msg = await bot.wait_for("message", check=chk, timeout=20)
        choice = int(msg.content.strip())
        if choice in opts:
            imp_count = choice
    except asyncio.TimeoutError:
        pass

    async with state.lock:
        state.started = True
        state.secret_word = random.choice(WORDS)
        ids = list(state.players.keys())
        state.imposters = set(random.sample(ids, k=imp_count))
        state.round_no = 0
        state.history.clear()
        state.voting_open = False
        state.votes.clear()
        state.expecting_clue_from = None

        # prevent double loops
        if state.game_task and not state.game_task.done():
            state.game_task.cancel()

    start_embed = e("🚀 Game started!", f"Order is fixed:\n{fmt_list(ctx.guild, state.join_order)}")
    start_embed.add_field(name="Next", value="Everyone click **Reveal Role** below (private).", inline=False)
    start_embed.add_field(name="Game length", value=f"{ROUNDS_BEFORE_FINAL_VOTE} rounds → final vote → reveal → end", inline=False)
    await ctx.send(embed=start_embed)
    await ctx.channel.send(
        embed=e("🎭 Reveal your role", "Click the button. Your role/word is shown only to you (ephemeral)."),
        view=RevealRoleView()
    )

    async with state.lock:
        state.game_task = asyncio.create_task(game_loop(ctx.guild, ctx.channel, state))


# =========================
# READY
# =========================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    bot.add_view(LobbyView())


bot.run(TOKEN)