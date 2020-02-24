import discord
from discord.ext import commands
import asyncio
import itertools
import sys
import traceback
from async_timeout import timeout
from functools import partial
from youtube_dl import YoutubeDL
import kkbox
import random
import json
from datetime import datetime

ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'geo-bypass': True,
    'no_warnings': False,
    'default_search': 'auto',
    'cachedir': False,
}
Downloaded_ffmpegopts = {
    'before_options': '-nostdin',
    'options': '-vn -af loudnorm=I=-16:TP=-1.5:LRA=11'
}
ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester
        self.title = data.get('title')
        self.web_url = data.get('webpage_url')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.

        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, islist=False):
        loop = loop or asyncio.get_event_loop()
        to_run = partial(ytdl.extract_info, url=search, download=True)
        data = await loop.run_in_executor(None, to_run)
        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]
        if islist is True:
            await ctx.send(f'```ini\n[{ctx.author.display_name} 新增 {data["title"]} 到佇列中]\n```', delete_after=10)
        source = ytdl.prepare_filename(data)
        return {'id': data['id'], 'webpage_url': data['webpage_url'], 'file_url': source, 'requester': ctx.author.display_name, 'title': data['title']}

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.

        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']
        return cls(discord.FFmpegPCMAudio(data['file_url'], **Downloaded_ffmpegopts), data=data, requester=requester)


class MusicPlayer:
    """A class which is assigned to each guild using the bot for Music.

    This class implements a queue and loop, which allows for different guilds to listen to different playlists
    simultaneously.

    When the bot disconnects from the Voice it's instance will be destroyed.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog',
                 'queue', 'next', 'current', 'np', 'volume')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.PriorityQueue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .1
        self.current = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
                    source = source[2]
            except asyncio.TimeoutError:
                return self.destroy(self._guild)

            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self._channel.send(f'There was an error processing your song.\n' f'```css\n[{e}]\n```')
                    await self._channel.send(f'{source}此首歌發生錯誤')
                    print(e)
                    continue
            self.current = source
            source.volume = self.volume

            self._guild.voice_client.play(
                source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            self.np = await self._channel.send(f'**正在播放:** `{source.title}` 由'
                                               f'`{source.requester}`點播')
            await self.np.add_reaction("⏯️")
            await self.np.add_reaction("⏭️")
            await self.np.add_reaction("⏹️")
            await self.np.add_reaction("🔊")
            await self.np.add_reaction("🔉")
            await self.np.add_reaction("📃")
            await self.np.add_reaction("🎵")
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            source.cleanup()
            self.current = None

            try:
                # We are no longer playing this song...
                await self.np.delete()
            except discord.HTTPException:
                pass

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.search_num = 5
        self.welcome = None

    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to Voice Channel. '
                           'Please make sure you are in a valid channel or provide me with one')

        print('Ignoring exception in command {}:'.format(
            ctx.command), file=sys.stderr)
        traceback.print_exception(
            type(error), error, error.__traceback__, file=sys.stderr)

    def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player

        return player

    @commands.command(name='connect', aliases=['join'])
    async def connect_(self, ctx, *, channel: discord.VoiceChannel = None):
        """Connect to voice.

        Parameters
        ------------
        channel: discord.VoiceChannel [Optional]
            The channel to connect to. If a channel is not specified, an attempt to join the voice channel you are in
            will be made.

        This command also handles moving the bot to different channels.
        """
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                raise InvalidVoiceChannel(
                    'No channel to join. Please either specify a valid channel or join one.')

        vc = ctx.voice_client
        if vc:
            if vc.channel.id == channel.id:
                return await ctx.send('我已經在頻道裡了')
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(
                    f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(
                    f'Connecting to channel: <{channel}> timed out.')

        player = self.get_player(ctx)
        welcome = await ctx.send(f'Connected to: **{channel}**,你能使用下面按鈕自動點歌', delete_after=20)
        await welcome.add_reaction("🎵")

    @commands.command(name='clean')
    async def clean_(self, ctx):
        """清空播放清單裡待播放的歌曲
        此指令會清空佇列和現在播放的歌曲，請小心使用
        """
        player = self.get_player(ctx)
        player.queue = asyncio.PriorityQueue()

    @commands.command(name='play', aliases=['sing', 'p', 'P'])
    async def play_(self, ctx, *, search: str):
        """新增歌曲至播放清單

        This command attempts to join a valid voice channel if the bot is not already in one.
        Uses YTDL to automatically search and retrieve a song.

        Parameters
        ------------
        search: str [Required]
            The song to search and retrieve using YTDL. This could be a simple search, an ID or URL.
        """
        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)
        player = self.get_player(ctx)
        # If download is False, source will be a dict which will be used later to regather the stream.
        # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
        with open('song.json', 'r') as f:
            SongList = json.loads(f.read())
        if ctx.author.id in SongList['ban']:
            return await ctx.send('你沒有權限點歌')

        try:
            source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
        except Exception as e:
            return await ctx.send(f'```ini\n[抱歉{ctx.author.display_name}，現在可能沒辦法提供點歌服務，請使用歌單指令]\n原因:{e}```')
        if source['id'] not in SongList['song']:
            SongList['song'][source['id']] = {'title': source['title'], 'url': source['webpage_url'],
                                              'requester': source['requester'], 'file_url': source['file_url']}
            with open('song.json', 'w', encoding='utf8') as f:
                json.dump(SongList, f, indent=4, ensure_ascii=False)
        await player.queue.put((5, datetime.now().timestamp(), source))
        return await ctx.send(f'```ini\n[{ctx.author.display_name} 新增 {source["title"]} 到佇列中]\n```', delete_after=10)

    @commands.command(name='add', aliases=['a'])
    async def add_(self, ctx, *, inputstr: str):
        """請輸入想要新增歌曲的語言ch,jp,en,kr,tw,hk和數量
        ex:!add ch 10
        """
        await ctx.trigger_typing()
        vc = ctx.voice_client
        if not vc:
            await ctx.invoke(self.connect_)
        player = self.get_player(ctx)
        songlang = inputstr.split(' ')[0]
        songnum = int(inputstr.split(' ')[1])
        if songnum > 50 or songnum < 0:
            return await ctx.send(f'**`{ctx.author.display_name}`**,請輸入1~50間的數字')
        SearchList = kkbox.search(songlang, songnum)
        for i in SearchList:
            source = await YTDLSource.create_source(ctx, i, loop=self.bot.loop, islist=True)
            await player.queue.put((10, datetime.now().timestamp(), source))
        return await ctx.send(f'```ini\n[{ctx.author.display_name}從新增{songnum}首歌]\n```')

    @commands.command(name='playlist', aliases=['pl'])
    async def playlist_(self, ctx, *, inputstr: str):
        """自定義歌單，務必閱讀使用方法！！
        此指令有許多子指令，請詳細閱讀用法
        ex:!pl list 列出自定義歌單所有歌曲
           !pl play 'number' 匯入指定歌單中第幾首歌曲
           !pl remove 'number' 刪除第幾首歌曲(從1開始數!!)
           !pl 'number' 從歌單中取出 'number' 首歌
        """
        command = inputstr.split(' ')[0]
        if command == 'list':
            with open('song.json', 'r') as f:
                SongList = json.loads(f.read())
            templist = [list(SongList['song'].keys())[i:i+20]
                        for i in range(0, len(SongList['song']), 20)]
            count = 0
            channel = ctx.channel
            for i in templist:
                fmt = '\n'.join(
                    f'**`{count*20+_+1}`**.**`{SongList["song"][i[_]]["title"]}`**' for _ in range(len(i)))
                embed = discord.Embed(
                    title=f'自定義清單 -總共有 {len(SongList["song"])}首歌-第{count*20+1}到{count*20+len(i)}', description=fmt)
                await ctx.send(embed=embed)
                count = count + 1
        elif command == 'play':
            with open('song.json', 'r') as f:
                SongList = json.loads(f.read())
            await ctx.trigger_typing()
            vc = ctx.voice_client
            if not vc:
                await ctx.invoke(self.connect_)
            player = self.get_player(ctx)
            # sepcific song
            if inputstr.split(' ')[1] is not None:
                try:
                    number = int(inputstr.split(' ')[1])
                except:
                    return await ctx.send(f"```ini\n[請輸入歌曲編號\n]```")
                Song = SongList['song'][list(
                    SongList['song'].keys())[number-1]]
                try:
                    if 'requester' not in Song:
                        Song['requester'] = ctx.author.display_name
                    await player.queue.put((5, datetime.now().timestamp(), Song))
                    return await ctx.send(f'```ini\n[{ctx.author.display_name} 新增 {Song["title"]} 到佇列中]\n```', delete_after=10)
                except Exception as e:
                    return await ctx.send(f"```ini\n[機器人發現 第{number}首-{SongList['song'][number-1]['title']} 此首歌存在錯誤,請手動刪除]\n原因:{str(e)[7:]}```")
                return await ctx.send(f"```ini\n[{ctx.author.display_name} 新增 {SongList['song'][number-1]['title']} 到佇列]\n```")
            else:
                return await ctx.send(f"```ini\n[因為歌單太大，現在不支援匯入全部歌單]\n```", delete_after=15)
        elif command == 'remove':
            if inputstr.split(' ')[1] is not None:
                with open('song.json', 'r') as f:
                    SongList = json.loads(f.read())
                if inputstr.split(' ')[1].isdigit() is True:
                    number = int(inputstr.split(' ')[1])
                    Remove_song = SongList['song'].pop(
                        list(SongList['song'].keys())[number-1])
                else:
                    song = ' '.join(inputstr.split(' ')[1:])
                    try:
                        source = await YTDLSource.create_source(ctx, song, loop=self.bot.loop)
                    except Exception as e:
                        print(e)
                        return
                    Remove_song = SongList['song'].pop(source['id'])
            else:
                return await ctx.send('remove 此功能的參數必須是數字或是歌名 ex:!pl remove "1 or 白月光"')
            with open('song.json', 'w', encoding='utf8') as f:
                json.dump(SongList, f, indent=4, ensure_ascii=False)
            return await ctx.send(f'```ini\n[{ctx.author.display_name} 從自定義播放清單中移除 {Remove_song["title"]}]\n```', delete_after=15)
        elif command.isdigit():
            with open('song.json', 'r') as f:
                SongList = json.loads(f.read())
            num = int(command)
            await ctx.trigger_typing()
            vc = ctx.voice_client
            if not vc:
                await ctx.invoke(self.connect_)
            player = self.get_player(ctx)
            random.seed(datetime.now().timestamp())
            temp_songlist = list(SongList['song'].keys())
            random.shuffle(temp_songlist)
            for i in temp_songlist[:num]:
                Song = SongList['song'][i]
                try:
                    if 'requester' not in Song:
                        Song['requester'] = ctx.author.display_name
                    await player.queue.put((10, datetime.now().timestamp(), Song))
                except Exception as e:
                    pass
            return await ctx.send(f'```ini\n[{ctx.author.display_name} 新增 {num}首歌到佇列]\n```')

    @commands.command(name='force', aliases=['f'])
    async def force_(self, ctx, *, search: str):
        """插歌指令,和play的差別只在這首歌會在佇列最上面
        """
        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = self.get_player(ctx)
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop)
        with open('song.json', 'r') as f:
            SongList = json.loads(f.read())
        if source['id'] not in SongList['song']:
            SongList['song'][source['id']] = {'title': source['title'], 'url': source['webpage_url'],
                                              'requester': source['requester'], 'file_url': source['file_url']}
            with open('song.json', 'w', encoding='utf8') as f:
                json.dump(SongList, f, indent=4, ensure_ascii=False)
        return await player.queue.put((1, datetime.now().timestamp(), source))

    @commands.command(name='pause')
    async def pause_(self, ctx):
        """暫停現在播放的歌曲"""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            return await ctx.send('最高品質靜悄悄', delete_after=20)
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send(f'**`{ctx.author.display_name}`**: 暫停了音樂')

    @commands.command(name='resume')
    async def resume_(self, ctx):
        """恢復播放"""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('最高品質靜悄悄', delete_after=20)
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send(f'**`{ctx.author.display_name}`**: 恢復播放!')

    @commands.command(name='skip')
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('最高品質靜悄悄', delete_after=20)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()
        await ctx.send(f'**`{ctx.author.display_name}`**: 跳過此首歌曲!', delete_after=15)

    @commands.command(name='queue', aliases=['q'])
    async def queue_info(self, ctx):
        """查看播放清單裡前10首歌"""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('最高品質靜悄悄', delete_after=20)

        player = self.get_player(ctx)
        if player.queue.empty():
            return await ctx.send('佇列已沒有任何歌曲，點歌阿')
        # Grab up to 15 entries from the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, 15))
        fmt = '\n'.join(f'**`{_[2]["title"]}`**' for _ in upcoming)
        embed = discord.Embed(
            title=f'即將播放 - 總共有{player.queue.qsize()}首 - Next {len(upcoming)}', description=fmt)

        await ctx.send(embed=embed, delete_after=20)

    @commands.command(name='now_playing', aliases=['np', 'current', 'currentsong', 'playing'])
    async def now_playing_(self, ctx):
        """顯示現在正在播放的歌曲"""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('最高品質靜悄悄', delete_after=20)

        player = self.get_player(ctx)
        if not player.current:
            return await ctx.send('最高品質靜悄悄', delete_after=20)

        try:
            # Remove our previous now_playing message.
            await player.np.delete()
        except discord.HTTPException:
            pass

        player.np = await ctx.send(f'**正在播放:** `{vc.source.title}` '
                                   f'由`{vc.source.requester}`點播')

    @commands.command(name='volume', aliases=['vol', 'v'])
    async def change_volume(self, ctx, *, vol: float):
        """調整音量 1~100

        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('最高品質靜悄悄', delete_after=20)

        if not 0 < vol < 101:
            return await ctx.send('請輸入介於1~100間的數字', delete_after=30)

        player = self.get_player(ctx)

        if vc.source:
            print('vc-source')
            vc.source.volume = vol / 100
        print('not in vc-source')
        player.volume = vol / 100
        await ctx.send(f'**`{ctx.author.display_name}`**: 將音量設定為 **{vol}%**', delete_after=30)

    @commands.command(name='members')
    async def show_members(self, ctx):
        """成員指令
        此指令會顯示出頻道內所有成員的uid
        """
        for i in ctx.guild.members:
            await ctx.send(i.display_name+'-'+str(i.id))

    @commands.command(name='ban')
    async def ban_members(self, ctx, *, inputid: int):
        """ban
        就是ban
        """
        if ctx.author.id != 211813274730233857:
            return await ctx.send('nono')
        else:
            with open('song.json') as f:
                user = json.loads(f.read())
            if inputid not in user['ban']:
                user['ban'].append(inputid)
                with open('song.json', 'w', encoding='utf8') as f:
                    json.dump(user, f, indent=4, ensure_ascii=False)
            else:
                return

    @commands.command(name='stop')
    async def stop_(self, ctx):
        """停止播放

        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            return await ctx.send('最高品質靜悄悄', delete_after=20)

        await self.cleanup(ctx.guild)

    @commands.command(pass_context=True)
    async def clear(self, ctx):
        """清除指令
        此指令會清除所有機器人發言和其他使用者發出指令的留言
        """
        def check(
            message): return message.author.id == bot.user.id or '!' in message.content
        await ctx.channel.purge(check=check, limit=100)


bot = commands.Bot(command_prefix=commands.when_mentioned_or(
    '!'), description='Made by Tamama\n痾 那個阿 歌單不小心在更新更失敗，所以都不見了')


@bot.event
async def on_raw_reaction_add(reaction):
    if reaction.member.bot:
        return
    else:
        emoji = reaction.emoji.name
        vc = reaction.member.guild.voice_client
        channel = discord.utils.get(
            reaction.member.guild.text_channels, id=reaction.channel_id)
        if emoji == "⏭️":
            await skip(vc, channel, reaction)
        elif emoji == "⏯️":
            await playorpause(vc)
        elif emoji == "⏹️":
            await stop(vc, channel, reaction)
        elif emoji == "🎵":
            await auto(vc, channel, reaction)
        elif emoji == "🔊":
            await louder(vc, channel, reaction)
        elif emoji == "🔉":
            await lower(vc, channel, reaction)
        elif emoji == "📃":
            await queue(vc, channel, reaction)


@bot.event
async def on_reaction_remove(reaction, user):
    if reaction.emoji == "⏯️":
        vc = user.guild.voice_client
        await playorpause(vc)


async def queue(vc, channel, reaction):
    player = Main_bot.get_player(ctx=reaction.member)
    if player.queue.empty():
        return await channel.send('佇列已沒有任何歌曲，點歌阿')

    upcoming = list(itertools.islice(player.queue._queue, 0, 15))
    fmt = '\n'.join(f'**`{_[2]["title"]}`**' for _ in upcoming)
    embed = discord.Embed(
        title=f'即將播放 - 總共有{player.queue.qsize()}首 - Next {len(upcoming)}', description=fmt)
    await channel.send(embed=embed)


async def louder(vc, channel, reaction):
    player = Main_bot.get_player(ctx=reaction.member)
    if vc.source:
        vc.source.volume = vc.source.volume + (2/100)

    player.volume = player.volume + (2/100)
    await channel.send(f'**`{reaction.member.display_name}`**: 將音量設定為 **{int(player.volume*100)}%**', delete_after=30)


async def lower(vc, channel, reaction):
    player = Main_bot.get_player(ctx=reaction.member)
    if vc.source:
        vc.source.volume = vc.source.volume - (2/100)

    player.volume = player.volume - (2/100)
    await channel.send(f'**`{reaction.member.display_name}`**: 將音量設定為 **{int(player.volume*100)}%**', delete_after=30)


async def skip(vc, channel, reaction):
    if not vc or not vc.is_connected():
        return await ctx.send('最高品質靜悄悄', delete_after=20)

    if vc.is_paused():
        pass
    elif not vc.is_playing():
        return

    vc.stop()
    await channel.send(f'**`{reaction.member.display_name}`**: 跳過此首歌曲!', delete_after=15)


async def playorpause(vc):
    if not vc or not vc.is_connected():
        return
    elif not vc.is_paused():
        vc.pause()
        return
    vc.resume()


async def stop(vc, channel, reaction):
    if not vc or not vc.is_connected():
        return await channel.send('最高品質靜悄悄', delete_after=20)

    await Main_bot.cleanup(guild=reaction.member.guild)
    def check(
        message): return message.author.id == bot.user.id or '!' in message.content
    await channel.purge(check=check, limit=100)


async def auto(vc, channel, reaction):
    with open('song.json', 'r') as f:
        SongList = json.loads(f.read())
    player = Main_bot.get_player(ctx=reaction.member)
    random.seed(datetime.now().timestamp())
    temp_songlist = list(SongList['song'].keys())
    random.shuffle(temp_songlist)
    for i in temp_songlist[:100]:
        Song = SongList['song'][i]
        try:
            if 'requester' not in Song:
                Song['requester'] = reaction.member.display_name
            await player.queue.put((10, datetime.now().timestamp(), Song))
        except Exception as e:
            pass
    return await channel.send(f'```ini\n[{reaction.member.display_name} 新增 100首歌到佇列]\n```', delete_after=20)


@bot.event
async def on_ready():
    print('Logged in as:\n{0} (ID: {0.id})'.format(bot.user))
# 此變數用來處理reaction,取得Music Player
Main_bot = Music(bot)
bot.add_cog(Main_bot)
with open('key.txt', 'r') as f:
    key = f.read()
bot.run(key.strip())
