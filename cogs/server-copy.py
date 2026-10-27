import asyncio
import io

import discord
from discord import app_commands
from discord.ext import commands

STICKER_EXT = {
    discord.StickerFormatType.png: "png",
    discord.StickerFormatType.apng: "png",
    discord.StickerFormatType.lottie: "json",
    discord.StickerFormatType.gif: "gif",
}


def _reason(e: Exception) -> str:
    """失敗理由を短い日本語の説明にする"""
    if isinstance(e, discord.Forbidden):
        return "権限不足(BOTの権限・ロール位置を確認)"
    if isinstance(e, discord.HTTPException):
        text = (e.text or "").replace("\n", " ")
        return f"HTTP {e.status} / code {e.code}: {text}"[:120]
    return f"{type(e).__name__}: {e}"[:120]


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="実行する", style=discord.ButtonStyle.danger)
    async def ok(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.edit_message(content="コピーしています…", view=None)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()


class ServerCopy(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.running: set[int] = set()  # コピー先サーバーIDの二重実行防止

    # ---------- 権限チェック ----------
    async def _is_admin(self, guild: discord.Guild, user_id: int) -> bool:
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.HTTPException:
            return False
        return member.guild_permissions.administrator

    # ---------- 各コピー処理 ----------
    async def _copy_roles(self, src: discord.Guild, dst: discord.Guild, role_map: dict):
        ok = 0
        fails: list[str] = []
        role_map[src.default_role.id] = dst.default_role
        for role in reversed(src.roles):  # 下位から作成
            if role.is_default() or role.managed:
                continue
            try:
                new = await dst.create_role(
                    name=role.name,
                    permissions=role.permissions,
                    colour=role.colour,
                    hoist=role.hoist,
                    mentionable=role.mentionable,
                    reason="サーバーコピー",
                )
                role_map[role.id] = new
                ok += 1
            except Exception as e:
                fails.append(f"{role.name}: {_reason(e)}")
            await asyncio.sleep(1)
        return ok, fails

    def _convert_overwrites(self, overwrites, role_map: dict):
        result = {}
        for target, ow in overwrites.items():
            if isinstance(target, discord.Role) and target.id in role_map:
                result[role_map[target.id]] = ow
        return result

    async def _copy_channels(self, src: discord.Guild, dst: discord.Guild, role_map: dict):
        ok = 0
        fails: list[str] = []
        cat_map: dict[int, discord.CategoryChannel] = {}

        # カテゴリ
        for cat in src.categories:
            try:
                new = await dst.create_category(
                    name=cat.name,
                    overwrites=self._convert_overwrites(cat.overwrites, role_map),
                    reason="サーバーコピー",
                )
                cat_map[cat.id] = new
                ok += 1
            except Exception as e:
                fails.append(f"カテゴリ {cat.name}: {_reason(e)}")
            await asyncio.sleep(1)

        # チャンネル(position順)
        for ch in sorted(src.channels, key=lambda c: c.position):
            if isinstance(ch, discord.CategoryChannel):
                continue
            category = cat_map.get(ch.category_id) if ch.category_id else None
            ow = self._convert_overwrites(ch.overwrites, role_map)
            try:
                if isinstance(ch, discord.TextChannel):
                    await dst.create_text_channel(
                        name=ch.name,
                        topic=ch.topic,
                        nsfw=ch.nsfw,
                        slowmode_delay=ch.slowmode_delay,
                        category=category,
                        overwrites=ow,
                        reason="サーバーコピー",
                    )
                elif isinstance(ch, discord.VoiceChannel):
                    await dst.create_voice_channel(
                        name=ch.name,
                        bitrate=min(ch.bitrate, int(dst.bitrate_limit)),
                        user_limit=ch.user_limit,
                        category=category,
                        overwrites=ow,
                        reason="サーバーコピー",
                    )
                elif isinstance(ch, discord.StageChannel):
                    await dst.create_stage_channel(
                        name=ch.name,
                        category=category,
                        overwrites=ow,
                        reason="サーバーコピー",
                    )
                elif isinstance(ch, discord.ForumChannel):
                    await dst.create_forum(
                        name=ch.name,
                        topic=ch.topic or "",
                        nsfw=ch.nsfw,
                        category=category,
                        overwrites=ow,
                        reason="サーバーコピー",
                    )
                else:
                    continue
                ok += 1
            except Exception as e:
                fails.append(f"#{ch.name}: {_reason(e)}")
            await asyncio.sleep(1)
        return ok, fails

    async def _copy_emojis(self, src: discord.Guild, dst: discord.Guild):
        ok = 0
        fails: list[str] = []
        free = dst.emoji_limit - len(dst.emojis)
        for emoji in src.emojis:
            if free <= 0:
                fails.append(f":{emoji.name}: コピー先の絵文字スロットが上限です")
                continue
            try:
                data = await emoji.read()
                await dst.create_custom_emoji(
                    name=emoji.name, image=data, reason="サーバーコピー"
                )
                ok += 1
                free -= 1
            except Exception as e:
                fails.append(f":{emoji.name}: {_reason(e)}")
            await asyncio.sleep(2)
        return ok, fails

    async def _copy_stickers(self, src: discord.Guild, dst: discord.Guild):
        ok = 0
        fails: list[str] = []
        free = dst.sticker_limit - len(dst.stickers)
        for st in src.stickers:
            if free <= 0:
                fails.append(f"{st.name}: コピー先のスタンプスロットが上限です")
                continue
            try:
                data = await st.read()
                ext = STICKER_EXT.get(st.format, "png")
                await dst.create_sticker(
                    name=st.name,
                    description=st.description or st.name,
                    emoji=st.emoji or "🙂",
                    file=discord.File(io.BytesIO(data), filename=f"{st.name}.{ext}"),
                    reason="サーバーコピー",
                )
                ok += 1
                free -= 1
            except Exception as e:
                fails.append(f"{st.name}: {_reason(e)}")
            await asyncio.sleep(2)
        return ok, fails

    # ---------- コマンド ----------
    @app_commands.command(
        name="server-copy",
        description="別サーバーの絵文字・スタンプ・チャンネル・ロールをこのサーバーへコピーします",
    )
    @app_commands.describe(
        source_id="コピー元サーバーのID",
        roles="ロールをコピー(チャンネル権限の再現に必要)",
        channels="カテゴリ・チャンネルをコピー",
        emojis="絵文字をコピー",
        stickers="スタンプをコピー",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def server_copy(
        self,
        interaction: discord.Interaction,
        source_id: str,
        roles: bool = True,
        channels: bool = True,
        emojis: bool = True,
        stickers: bool = True,
    ):
        dst = interaction.guild

        # コピー先の管理者チェック
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message(
                "このコマンドは管理者のみ使用できます。", ephemeral=True
            )

        if not source_id.isdigit():
            return await interaction.response.send_message(
                "サーバーIDは数字で指定してください。", ephemeral=True
            )
        src = self.bot.get_guild(int(source_id))
        if src is None:
            return await interaction.response.send_message(
                "コピー元サーバーが見つかりません。BOTをコピー元サーバーにも招待してください。",
                ephemeral=True,
            )
        if src.id == dst.id:
            return await interaction.response.send_message(
                "コピー元とコピー先が同じです。", ephemeral=True
            )

        # コピー元の管理者チェック(他人のサーバーを勝手にコピーさせない)
        if not await self._is_admin(src, interaction.user.id):
            return await interaction.response.send_message(
                "コピー元サーバーの管理者権限が必要です。", ephemeral=True
            )

        if dst.id in self.running:
            return await interaction.response.send_message(
                "このサーバーではすでにコピーが実行中です。", ephemeral=True
            )

        targets = [
            n for n, f in
            (("ロール", roles), ("チャンネル", channels), ("絵文字", emojis), ("スタンプ", stickers))
            if f
        ]
        if not targets:
            return await interaction.response.send_message(
                "コピーする項目を1つ以上選んでください。", ephemeral=True
            )

        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"**{src.name}** → **{dst.name}** へコピーします。\n"
            f"対象: {' / '.join(targets)}\n"
            "できるだけロールを最上位に配置してください。BOTのロールより上のロールがコピーされない場合があります\n※既存のものは削除せず、追加のみ行います。よろしいですか?",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.value:
            return

        self.running.add(dst.id)
        report = []
        try:
            role_map: dict = {}
            if roles:
                ok, fails = await self._copy_roles(src, dst, role_map)
                report.append(("ロール", ok, fails))
            if channels:
                if not role_map:
                    role_map[src.default_role.id] = dst.default_role
                ok, fails = await self._copy_channels(src, dst, role_map)
                report.append(("チャンネル", ok, fails))
            if emojis:
                ok, fails = await self._copy_emojis(src, dst)
                report.append(("絵文字", ok, fails))
            if stickers:
                ok, fails = await self._copy_stickers(src, dst)
                report.append(("スタンプ", ok, fails))
        finally:
            self.running.discard(dst.id)

        layouts = self._result_views(src, dst, report)
        for layout in layouts:
            try:
                await interaction.followup.send(view=layout, ephemeral=True)
            except discord.HTTPException:
                # 15分を超えてトークンが失効した場合
                await interaction.channel.send(view=layout)

    def _result_views(self, src: discord.Guild, dst: discord.Guild, report: list):
        """失敗を全件表示する。文字数上限を超える分は複数メッセージに分割する"""
        total_fail = sum(len(f) for _, _, f in report)

        # 全行をフラットに並べる
        lines: list[str] = []
        for label, ok, fails in report:
            lines.append(f"**{label}**: 成功 {ok} / 失敗 {len(fails)}")
            for f in fails:
                lines.append(f"- ❌ {f}"[:300])

        # 1メッセージあたりの本文を3000字以内に分割
        chunks: list[list[str]] = [[]]
        size = 0
        for line in lines:
            if size + len(line) + 1 > 3000 and chunks[-1]:
                chunks.append([])
                size = 0
            chunks[-1].append(line)
            size += len(line) + 1

        colour = discord.Colour.orange() if total_fail else discord.Colour.green()
        layouts = []
        for i, chunk in enumerate(chunks):
            layout = discord.ui.LayoutView()
            container = discord.ui.Container(accent_colour=colour)
            if i == 0:
                title = (
                    "## ⚠️ サーバーコピー完了(一部失敗)"
                    if total_fail
                    else "## ✅ サーバーコピー完了"
                )
                container.add_item(discord.ui.TextDisplay(title))
                container.add_item(discord.ui.Separator())
                container.add_item(
                    discord.ui.TextDisplay(f"**{src.name}** → **{dst.name}**")
                )
            else:
                container.add_item(
                    discord.ui.TextDisplay(f"## 続き ({i + 1}/{len(chunks)})")
                )
                container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay("\n".join(chunk)))
            layout.add_item(container)
            layouts.append(layout)
        return layouts


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerCopy(bot))
