import os
import sys
import time
import shlex
import shutil
import random
import inspect
import logging
import asyncio
import pathlib
import traceback
import math
import re

import aiohttp
import discord
import colorlog

from io import BytesIO, StringIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta
from collections import defaultdict

from discord.enums import ChannelType

from . import exceptions
from . import downloader

from .playlist import Playlist
from .player import MusicPlayer
from .entry import StreamPlaylistEntry
from .opus_loader import load_opus_lib
from .config import Config, ConfigDefaults
from .permissions import Permissions, PermissionsDefaults
from .constructs import SkipState, Response
from .utils import load_file, write_file, fixg, ftimedelta, _func_, _get_variable
from .spotify import Spotify
from .json import Json

from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH


load_opus_lib()

log = logging.getLogger(__name__)


class MusicBot(discord.Client):
    def __init__(self, config_file=None, perms_file=None):
        try:
            sys.stdout.write("\x1b]2;MusicBot {}\x07".format(BOTVERSION))
        except:
            pass

        print()

        if config_file is None:
            config_file = ConfigDefaults.options_file

        if perms_file is None:
            perms_file = PermissionsDefaults.perms_file

        self.players = {}
        self.exit_signal = None
        self.init_ok = False
        self.cached_app_info = None
        self.last_status = None

        self.config = Config(config_file)
        
        self._setup_logging()
        
        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])
        self.str = Json(self.config.i18n_file)

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)

        self.aiolocks = defaultdict(asyncio.Lock)
        self.downloader = downloader.Downloader(download_folder='audio_cache')

        log.info('warota discord bot バージョン{} を起動します。'.format(BOTVERSION))

        if not self.autoplaylist:
            log.warning("自動プレイリストは空ですので、無効化します。")
            self.config.auto_playlist = False
        else:
            log.info("自動プレイリストを読み込みました。{}つの項目が登録されています。".format(len(self.autoplaylist)))

        if self.blacklist:
            log.debug("ブラックリストを読み込みました。{}つの項目が登録されています。".format(len(self.blacklist)))

        # TODO: Do these properly
        ssd_defaults = {
            'last_np_msg': None,
            'auto_paused': False,
            'availability_paused': False
        }
        self.server_specific_data = defaultdict(ssd_defaults.copy)

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

        self.spotify = None
        if self.config._spotify:
            try:
                self.spotify = Spotify(self.config.spotify_clientid, self.config.spotify_clientsecret, aiosession=self.aiosession, loop=self.loop)
                if not self.spotify.token:
                    log.warning('Spotify に接続できませんでした。')
                    self.config._spotify = False
                else:
                    log.info('Spotify に接続しました。')
            except exceptions.SpotifyError as e:
                log.warning('Spotify に接続できませんでした。 詳細: {0} 5秒後に続行します。'.format(e))
                self.config._spotify = False
                time.sleep(5)  # make sure they see the problem

    # TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("管理者のみがそのコマンドを使用できます。", expire_in=30)

        return wrapper

    def dev_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            orig_msg = _get_variable('message')

            if str(orig_msg.author.id) in self.config.dev_ids:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("開発者のみがそのコマンドを使用できます。", expire_in=30)

        wrapper.dev_cmd = True
        return wrapper

    def ensure_appinfo(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            await self._cache_app_info()
            # noinspection PyCallingNonCallable
            return await func(self, *args, **kwargs)

        return wrapper

    def _get_owner(self, *, server=None, voice=False):
            return discord.utils.find(
                lambda m: m.id == self.config.owner_id and (m.voice if voice else True),
                server.members if server else self.get_all_members()
            )

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    def _setup_logging(self):
        if len(logging.getLogger(__package__).handlers) > 1:
            log.debug("ロガーのセットアップを飛ばします。すでに設定されています。")
            return

        shandler = logging.StreamHandler(stream=sys.stdout)
        shandler.setFormatter(colorlog.LevelFormatter(
            fmt = {
                'DEBUG': '{log_color}[{levelname}:{module}] {message}',
                'INFO': '{log_color}{message}',
                'WARNING': '{log_color}{levelname}: {message}',
                'ERROR': '{log_color}[{levelname}:{module}] {message}',
                'CRITICAL': '{log_color}[{levelname}:{module}] {message}',

                'EVERYTHING': '{log_color}[{levelname}:{module}] {message}',
                'NOISY': '{log_color}[{levelname}:{module}] {message}',
                'VOICEDEBUG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}',
                'FFMPEG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}'
            },
            log_colors = {
                'DEBUG':    'cyan',
                'INFO':     'white',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'bold_red',

                'EVERYTHING': 'white',
                'NOISY':      'white',
                'FFMPEG':     'bold_purple',
                'VOICEDEBUG': 'purple',
        },
            style = '{',
            datefmt = ''
        ))
        shandler.setLevel(self.config.debug_level)
        logging.getLogger(__package__).addHandler(shandler)

        log.debug("Set logging level to {}".format(self.config.debug_level_str))

        if self.config.debug_mode:
            dlogger = logging.getLogger('discord')
            dlogger.setLevel(logging.DEBUG)
            dhandler = logging.FileHandler(filename='logs/discord.log', encoding='utf-8', mode='w')
            dhandler.setFormatter(logging.Formatter('{asctime}:{levelname}:{name}: {message}', style='{'))
            dlogger.addHandler(dhandler)

    @staticmethod
    def _check_if_empty(vchannel: discord.abc.GuildChannel, *, excluding_me=True, excluding_deaf=False):
        def check(member):
            if excluding_me and member == vchannel.guild.me:
                return False

            if excluding_deaf and any([member.deaf, member.self_deaf]):
                return False

            return True

        return not sum(1 for m in vchannel.members if check(m))

    async def _join_startup_channels(self, channels, *, autosummon=True):
        joined_servers = set()
        channel_map = {c.guild: c for c in channels}

        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                log.info("Initial autopause in empty channel")

                player.pause()
                self.server_specific_data[player.voice_client.channel.guild]['auto_paused'] = True

        for guild in self.guilds:
            if guild.unavailable or guild in channel_map:
                continue

            if guild.me.voice:
                log.info("Found resumable voice channel {0.guild.name}/{0.name}".format(guild.me.voice.channel))
                channel_map[guild] = guild.me.voice.channel

            if autosummon:
                owner = self._get_owner(server=guild, voice=True)
                if owner:
                    log.info("Found owner in \"{}\"".format(owner.voice.channel.name))
                    channel_map[guild] = owner.voice.channel

        for guild, channel in channel_map.items():
            if guild in joined_servers:
                log.info("すでに \"{}\" へ参加しています。スキップします。".format(guild.name))
                continue

            if channel and isinstance(channel, discord.VoiceChannel):
                log.info("{0.guild.name}/{0.name} に接続しようとしています...".format(channel))

                chperms = channel.permissions_for(guild.me)

                if not chperms.connect:
                    log.info("\"{}\" に接続できません。参加する権限がありません。".format(channel.name))
                    continue

                elif not chperms.speak:
                    log.info("\"{}\" に接続できません。音声を発信する権限がありません。".format(channel.name))
                    continue

                try:
                    player = await self.get_player(channel, create=True, deserialize=self.config.persistent_queue)
                    joined_servers.add(guild)

                    log.info("{0.guild.name}/{0.name} へ参加しました".format(channel))

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist:
                        if self.config.auto_pause:
                            player.once('play', lambda player, **_: _autopause(player))
                        if not player.playlist.entries:
                            await self.on_player_finished_playing(player)

                except Exception:
                    log.debug("{0.guild.name}/{0.name} へ参加できませんでした。何か問題が発生しました。".format(channel), exc_info=True)
                    log.error("{0.guild.name}/{0.name} へ参加できませんでした。何か問題が発生しました。".format(channel))

            elif channel:
                log.warning("{0.guild.name}/{0.name} へ参加できませんでした。テキストチャンネルです。".format(channel))

            else:
                log.warning("チャンネルの設定エラー: {}".format(channel))

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message, quiet=True)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        if msg.guild.me.voice:
            vc = msg.guild.me.voice.channel
        else:
            vc = None

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or (msg.author.voice and vc == msg.author.voice.channel):
            return True
        else:
            raise exceptions.PermissionsError(
                "`%s` は音声チャンネルへ参加しているときのみ、ご利用いただけます。" % vc.name, expire_in=30)

    async def _cache_app_info(self, *, update=False):
        if not self.cached_app_info and not update and self.user.bot:
            log.debug("app の情報をキャッシュしています。")
            self.cached_app_info = await self.application_info()

        return self.cached_app_info


    async def remove_from_autoplaylist(self, song_url:str, *, ex:Exception=None, delete_from_ap=False):
        if song_url not in self.autoplaylist:
            log.debug("URL \"{}\" はプレイリストではありません。無視します。".format(song_url))
            return

        async with self.aiolocks[_func_()]:
            self.autoplaylist.remove(song_url)
            log.info("再生できない項目を削除しました: %s" % song_url)

            with open(self.config.auto_playlist_removed_file, 'a', encoding='utf8') as f:
                f.write(
                    '# Entry removed {ctime}\n'
                    '# Reason: {ex}\n'
                    '{url}\n\n{sep}\n\n'.format(
                        ctime=time.ctime(),
                        ex=str(ex).replace('\n', '\n#' + ' ' * 10), # 10 spaces to line up with # Reason:
                        url=song_url,
                        sep='#' * 32
                ))

            if delete_from_ap:
                log.info("自動プレイリストを更新中")
                write_file(self.config.auto_playlist_file, self.autoplaylist)

    @ensure_appinfo
    async def generate_invite_link(self, *, permissions=discord.Permissions(70380544), guild=None):
        return discord.utils.oauth_url(self.cached_app_info.id, permissions=permissions, guild=guild)

    async def get_voice_client(self, channel: discord.abc.GuildChannel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if not isinstance(channel, discord.VoiceChannel):
            raise AttributeError('Channel passed must be a voice channel')

        if channel.guild.voice_client:
            return channel.guild.voice_client
        else:
            return await channel.connect(timeout=60, reconnect=True)

    async def disconnect_voice_client(self, guild):
        vc = self.voice_client_in(guild)
        if not vc:
            return

        if guild.id in self.players:
            self.players.pop(guild.id).kill()

        await vc.disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in list(self.voice_clients).copy():
            await self.disconnect_voice_client(vc.channel.guild)

    async def set_voice_state(self, vchannel, *, mute=False, deaf=False):
        if isinstance(vchannel, discord.Object):
            vchannel = self.get_channel(vchannel.id)

        if getattr(vchannel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        await self.ws.voice_state(vchannel.guild.id, vchannel.id, mute, deaf)
        # I hope I don't have to set the channel here
        # instead of waiting for the event to update it

    def get_player_in(self, guild:discord.Guild) -> MusicPlayer:
        return self.players.get(guild.id)

    async def get_player(self, channel, create=False, *, deserialize=False) -> MusicPlayer:
        guild = channel.guild

        async with self.aiolocks[_func_() + ':' + str(guild.id)]:
            if deserialize:
                voice_client = await self.get_voice_client(channel)
                player = await self.deserialize_queue(guild, voice_client)

                if player:
                    log.debug("Created player via deserialization for guild %s with %s entries", guild.id, len(player.playlist))
                    # Since deserializing only happens when the bot starts, I should never need to reconnect
                    return self._init_player(player, guild=guild)

            if guild.id not in self.players:
                if not create:
                    raise exceptions.CommandError(
                        'ボットは音声チャンネルに参加していません。'
                        '%ssummon を使用して参加させてください。' % self.config.command_prefix)

                voice_client = await self.get_voice_client(channel)

                playlist = Playlist(self)
                player = MusicPlayer(self, voice_client, playlist)
                self._init_player(player, guild=guild)

        return self.players[guild.id]

    def _init_player(self, player, *, guild=None):
        player = player.on('play', self.on_player_play) \
                       .on('resume', self.on_player_resume) \
                       .on('pause', self.on_player_pause) \
                       .on('stop', self.on_player_stop) \
                       .on('finished-playing', self.on_player_finished_playing) \
                       .on('entry-added', self.on_player_entry_added) \
                       .on('error', self.on_player_error)

        player.skip_state = SkipState()

        if guild:
            self.players[guild.id] = player

        return player

    async def on_player_play(self, player, entry):
        log.debug('Running on_player_play')
        await self.update_now_playing_status(entry)
        player.skip_state.reset()

        # This is the one event where its ok to serialize autoplaylist entries
        await self.serialize_queue(player.voice_client.channel.guild)

        if self.config.write_current_song:
            await self.write_current_song(player.voice_client.channel.guild, entry)

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            last_np_msg = self.server_specific_data[channel.guild]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in channel.history(limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        await self.safe_delete_message(last_np_msg)
                        self.server_specific_data[channel.guild]['last_np_msg'] = None
                    break  # This is probably redundant

            author_perms = self.permissions.for_user(author)

            if author not in player.voice_client.channel.members and author_perms.skip_when_absent:
                newmsg = 'Skipping next song in `%s`: `%s` added by `%s` as queuer not in voice' % (
                    player.voice_client.channel.name, entry.title, entry.meta['author'].name)
                player.skip()
            elif self.config.now_playing_mentions:
                newmsg = '`%s` が再生されます... (@ `%s`)' % (entry.title, player.voice_client.channel.name)
            else:
                newmsg = '再生中 （`%s`）: `%s` by `%s`' % (
                    player.voice_client.channel.name, entry.title, entry.meta['author'].name)

            if self.server_specific_data[channel.guild]['last_np_msg']:
                self.server_specific_data[channel.guild]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
            else:
                self.server_specific_data[channel.guild]['last_np_msg'] = await self.safe_send_message(channel, newmsg)

        # TODO: Check channel voice state?

    async def on_player_resume(self, player, entry, **_):
        log.debug('Running on_player_resume')
        await self.update_now_playing_status(entry)

    async def on_player_pause(self, player, entry, **_):
        log.debug('Running on_player_pause')
        await self.update_now_playing_status(entry, True)
        # await self.serialize_queue(player.voice_client.channel.guild)

    async def on_player_stop(self, player, **_):
        log.debug('Running on_player_stop')
        await self.update_now_playing_status()

    async def on_player_finished_playing(self, player, **_):
        log.debug('Running on_player_finished_playing')
        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                log.info("Player finished playing, autopaused in empty channel")

                player.pause()
                self.server_specific_data[player.voice_client.channel.guild]['auto_paused'] = True

        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
            if not player.autoplaylist:
                if not self.autoplaylist:
                    # TODO: When I add playlist expansion, make sure that's not happening during this check
                    log.warning("再生できる項目がありません。自動プレイリストを無効化します。")
                    self.config.auto_playlist = False
                else:
                    log.debug("No content in current autoplaylist. Filling with new music...")
                    player.autoplaylist = list(self.autoplaylist)

            while player.autoplaylist:
                if self.config.auto_playlist_random:
                    random.shuffle(player.autoplaylist)
                    song_url = random.choice(player.autoplaylist)
                else:
                    song_url = player.autoplaylist[0]
                player.autoplaylist.remove(song_url)

                info = {}

                try:
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                except downloader.youtube_dl.utils.DownloadError as e:
                    if 'YouTube からのメッセージ:' in e.args[0]:
                        # url is bork, remove from list and put in removed list
                        log.error("無効なURL:\n{}".format(e.args[0]))

                    else:
                        # Probably an error from a different extractor, but I've only seen youtube's
                        log.error("無効なURL （\"{url}\"）: {ex}".format(url=song_url, ex=e))

                    await self.remove_from_autoplaylist(song_url, ex=e, delete_from_ap=self.config.remove_ap)
                    continue

                except Exception as e:
                    log.error("無効なURL （\"{url}\"）: {ex}".format(url=song_url, ex=e))
                    log.exception()

                    self.autoplaylist.remove(song_url)
                    continue

                if info.get('entries', None):  # or .get('_type', '') == 'playlist'
                    log.debug("Playlist found but is unsupported at this time, skipping.")
                    # TODO: Playlist expansion

                # Do I check the initial conditions again?
                # not (not player.playlist.entries and not player.current_entry and self.config.auto_playlist)

                if self.config.auto_pause:
                    player.once('play', lambda player, **_: _autopause(player))

                try:
                    await player.playlist.add_entry(song_url, channel=None, author=None)
                except exceptions.ExtractionError as e:
                    log.error("自動プレイリストに追加しているときに、エラーが発生しました: {}".format(e))
                    log.debug('', exc_info=True)
                    continue

                break

            if not self.autoplaylist:
                # TODO: When I add playlist expansion, make sure that's not happening during this check
                log.warning("再生できる曲がプレイリストにありません。無視します。")
                self.config.auto_playlist = False

        else: # Don't serialize for autoplaylist events
            await self.serialize_queue(player.voice_client.channel.guild)

        if not player.is_stopped and not player.is_dead:
            player.play(_continue=True)

    async def on_player_entry_added(self, player, playlist, entry, **_):
        log.debug('Running on_player_entry_added')
        if entry.meta.get('author') and entry.meta.get('channel'):
            await self.serialize_queue(player.voice_client.channel.guild)

    async def on_player_error(self, player, entry, ex, **_):
        if 'channel' in entry.meta:
            await self.safe_send_message(
                entry.meta['channel'],
                "```\nError from FFmpeg:\n{}\n```".format(ex)
            )
        else:
            log.exception("Player error", exc_info=ex)

    async def update_now_playing_status(self, entry=None, is_paused=False):
        game = None

        if not self.config.status_message:
            if self.user.bot:
                activeplayers = sum(1 for p in self.players.values() if p.is_playing)
                if activeplayers > 1:
                    game = discord.Game(type=0, name="music on %s guilds" % activeplayers)
                    entry = None

                elif activeplayers == 1:
                    player = discord.utils.get(self.players.values(), is_playing=True)
                    entry = player.current_entry

            if entry:
                prefix = u'\u275A\u275A ' if is_paused else ''

                name = u'{}{}'.format(prefix, entry.title)[:128]
                game = discord.Game(type=0, name=name)
        else:
            game = discord.Game(type=0, name=self.config.status_message.strip()[:128])

        async with self.aiolocks[_func_()]:
            if game != self.last_status:
                await self.change_presence(activity=game)
                self.last_status = game

    async def update_now_playing_message(self, guild, message, *, channel=None):
        lnp = self.server_specific_data[guild]['last_np_msg']
        m = None

        if message is None and lnp:
            await self.safe_delete_message(lnp, quiet=True)

        elif lnp:  # If there was a previous lp message
            oldchannel = lnp.channel

            if lnp.channel == oldchannel:  # If we have a channel to update it in
                async for lmsg in lnp.channel.history(limit=1):
                    if lmsg != lnp and lnp:  # If we need to resend it
                        await self.safe_delete_message(lnp, quiet=True)
                        m = await self.safe_send_message(channel, message, quiet=True)
                    else:
                        m = await self.safe_edit_message(lnp, message, send_if_fail=True, quiet=False)

            elif channel: # If we have a new channel to send it to
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(channel, message, quiet=True)

            else:  # we just resend it in the old channel
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(oldchannel, message, quiet=True)

        elif channel: # No previous message
            m = await self.safe_send_message(channel, message, quiet=True)

        self.server_specific_data[guild]['last_np_msg'] = m


    async def serialize_queue(self, guild, *, dir=None):
        """
        Serialize the current queue for a server's player to json.
        """

        player = self.get_player_in(guild)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/queue.json' % guild.id

        async with self.aiolocks['queue_serialization' + ':' + str(guild.id)]:
            log.debug("Serializing queue for %s", guild.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(player.serialize(sort_keys=True))

    async def serialize_all_queues(self, *, dir=None):
        coros = [self.serialize_queue(s, dir=dir) for s in self.guilds]
        await asyncio.gather(*coros, return_exceptions=True)

    async def deserialize_queue(self, guild, voice_client, playlist=None, *, dir=None) -> MusicPlayer:
        """
        Deserialize a saved queue for a server into a MusicPlayer.  If no queue is saved, returns None.
        """

        if playlist is None:
            playlist = Playlist(self)

        if dir is None:
            dir = 'data/%s/queue.json' % guild.id

        async with self.aiolocks['queue_serialization' + ':' + str(guild.id)]:
            if not os.path.isfile(dir):
                return None

            log.debug("Deserializing queue for %s", guild.id)

            with open(dir, 'r', encoding='utf8') as f:
                data = f.read()

        return MusicPlayer.from_json(data, self, voice_client, playlist)

    async def write_current_song(self, guild, entry, *, dir=None):
        """
        Writes the current song to file
        """
        player = self.get_player_in(guild)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/current.txt' % guild.id

        async with self.aiolocks['current_song' + ':' + str(guild.id)]:
            log.debug("Writing current song for %s", guild.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(entry.title)

    @ensure_appinfo
    async def _on_ready_sanity_checks(self):
        # Ensure folders exist
        await self._scheck_ensure_env()

        # Server permissions check
        await self._scheck_server_permissions()

        # playlists in autoplaylist
        await self._scheck_autoplaylist()

        # config/permissions async validate?
        await self._scheck_configs()


    async def _scheck_ensure_env(self):
        log.debug("データフォルダーを確保します。")
        for guild in self.guilds:
            pathlib.Path('data/%s/' % guild.id).mkdir(exist_ok=True)

        with open('data/server_names.txt', 'w', encoding='utf8') as f:
            for guilds in sorted(self.guilds, key=lambda s:int(s.id)):
                f.write('{:<22} {}\n'.format(guild.id, guild.name))

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                log.debug("古いキャッシュを削除しました。")
            else:
                log.debug("キャッシュを削除できませんでした。移動させます。")


    async def _scheck_server_permissions(self):
        log.debug("Checking server permissions")
        pass # TODO

    async def _scheck_autoplaylist(self):
        log.debug("Auditing autoplaylist")
        pass # TODO

    async def _scheck_configs(self):
        log.debug("Validating config")
        await self.config.async_validate(self)

        log.debug("Validating permissions config")
        await self.permissions.async_validate(self)



#######################################################################################################################


    async def safe_send_message(self, dest, content, **kwargs):
        tts = kwargs.pop('tts', False)
        quiet = kwargs.pop('quiet', False)
        expire_in = kwargs.pop('expire_in', 0)
        allow_none = kwargs.pop('allow_none', True)
        also_delete = kwargs.pop('also_delete', None)

        msg = None
        lfunc = log.debug if quiet else log.warning

        try:
            if content is not None or allow_none:
                if isinstance(content, discord.Embed):
                    msg = await dest.send(embed=content)
                else:
                    msg = await dest.send(content, tts=tts)

        except discord.Forbidden:
            lfunc("\"%s\"へメッセージを送信できません。権限がありません。", dest.name)

        except discord.NotFound:
            lfunc("\"%s\"へメッセージを送信できません。チャンネル名が無効です。", dest.name)

        except discord.HTTPException:
            if len(content) > DISCORD_MSG_CHAR_LIMIT:
                lfunc("メッセージが長すぎます。 (%s)", DISCORD_MSG_CHAR_LIMIT)
            else:
                lfunc("メッセージを送信できません。")
                log.noise("Got HTTPException trying to send message to %s: %s", dest, content)

        finally:
            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await message.delete()

        except discord.Forbidden:
            lfunc("メッセージ \"{}\" を削除できません。権限がありません。".format(message.clean_content))

        except discord.NotFound:
            lfunc("メッセージ \"{}\" を削除できません。見つかりませんでした。".format(message.clean_content))

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await message.edit(content=new)

        except discord.NotFound:
            lfunc("メッセージ \"{}\" を編集できません。見つかりませんでした。".format(message.clean_content))
            if send_if_fail:
                lfunc("代わりに再度送信します。")
                return await self.safe_send_message(message.channel, new)

    async def send_typing(self, destination):
        try:
            return await destination.trigger_typing()
        except discord.Forbidden:
            log.warning("{} へメッセージを送信中であることを通知できませんでした。権限がありません。}".format(destination))

    async def restart(self):
        self.exit_signal = exceptions.RestartSignal()
        await self.logout()

    def restart_threadsafe(self):
        asyncio.run_coroutine_threadsafe(self.restart(), self.loop)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
            self.loop.run_until_complete(self.aiosession.close())
        except: pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except: pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "ボットは Discord へログインできませんでした。",
                "token が間違っています。修正してください。"
            )  #     ^^^^ In theory self.config.auth should never have no items

        finally:
            try:
                self._cleanup()
            except Exception:
                log.error("ファイルを掃除できませんでした。", exc_info=True)

            if self.exit_signal:
                raise self.exit_signal # pylint: disable=E0702

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            log.error("エラー {}:\n{}".format(event, ex.message))

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            log.error("エラー {}".format(event), exc_info=True)

    async def on_resumed(self):
        log.info("\nDiscord へ再接続しました。\n")

    async def on_ready(self):
        dlogger = logging.getLogger('discord')
        for h in dlogger.handlers:
            if getattr(h, 'terminator', None) == '':
                dlogger.removeHandler(h)
                print()

        log.debug("接続が確立されました。準備ができました。")

        self.ws._keep_alive.name = 'Gateway Keepalive'

        if self.init_ok:
            log.debug("Received additional READY event, may have failed to resume")
            return

        await self._on_ready_sanity_checks()

        self.init_ok = True

        ################################

        log.info("ボット アカウント: {0}/{1}#{2}".format(
            self.user.id,
            self.user.name,
            self.user.discriminator
        ))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.guilds:
            log.info("管理者 アカウント:     {0}/{1}#{2}\n".format(
                owner.id,
                owner.name,
                owner.discriminator
            ))

            log.info('サーバー一覧:')
            unavailable_servers = 0
            for s in self.guilds:
                ser = ('{} (unavailable)'.format(s.name) if s.unavailable else s.name)
                log.info(' - ' + ser)
                if self.config.leavenonowners:
                    if s.unavailable:
                        unavailable_servers += 1
                    else:
                        check = s.get_member(owner.id)
                        if check == None:
                            await s.leave()
                            log.info('{} を退出しました。管理者がいません。'.format(s.name))
            if unavailable_servers != 0:
                log.info('Not proceeding with checks in {} servers due to unavailability'.format(str(unavailable_servers))) 

        elif self.guilds:
            log.warning("どのサーバーにも管理者が見つかりませんでした。 (ID: %s)\n" % self.config.owner_id)

            log.info('サーバー一覧:')
            for s in self.guilds:
                ser = ('{} (利用不可)'.format(s.name) if s.unavailable else s.name)
                log.info(' - ' + ser)

        else:
            log.warning("管理者がいない上に、ボットはどこのサーバーにも参加していません。")
            if self.user.bot:
                log.warning(
                    "ボットをサーバーに参加させるには、こちらのリンクを開いてください。 \n"
                    "注意: Discord にログイン済みで、かつサーバーの管理権限を所持している \n"
                    "必要があります。\n" + await self.generate_invite_link()
                )

        print(flush=True)

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if isinstance(c, discord.VoiceChannel))

            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            if chlist:
                log.info("認識されているテキストチャンネル: ")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                print("どのテキストチャンネルにも反応しません。")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("音声チャンネルに反応しません:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print(flush=True)

        else:
            log.info("どのテキストチャンネルにも反応しません。")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if isinstance(c, discord.TextChannel))

            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            if chlist:
                log.info("自動的に参加する音声チャンネル:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                log.info("どのチャンネルにも自動的には参加しません。")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("テキストチャンネルへ自動的に参加できませんでした:")
                [log.info(' - {}/{}'.format(ch.guild.name.strip(), ch.name.strip())) for ch in invalids if ch]

            self.autojoin_channels = chlist

        else:
            log.info("どのチャンネルにも自動的には参加しません。")
            self.autojoin_channels = set()
        
        if self.config.show_config_at_start:
            print(flush=True)
            log.info("設定:")

            log.info("  コマンドの接頭辞: " + self.config.command_prefix)
            log.info("  既定の音量: {}%".format(int(self.config.default_volume * 100)))
            log.info("  スキップ条件: {}個の有効票 または {}%".format(
                self.config.skips_required, fixg(self.config.skip_ratio_required * 100)))
            log.info("  再生中 @mentions: " + ['無効', '有効'][self.config.now_playing_mentions])
            log.info("  自動参加: " + ['無効', '有効'][self.config.auto_summon])
            log.info("  自動プレイリスト: " + ['無効', '有効'][self.config.auto_playlist] + " (order: " + ['sequential', 'random'][self.config.auto_playlist_random] + ")")
            log.info("  自動ポーズ: " + ['無効', '有効'][self.config.auto_pause])
            log.info("  メッセージの削除: " + ['無効', '有効'][self.config.delete_messages])
            if self.config.delete_messages:
                log.info("    呼び出しを削除: " + ['無効', '有効'][self.config.delete_invoking])
            log.info("  デバッグ: " + ['無効', '有効'][self.config.debug_mode])
            log.info("  ダウンロードされた項目は " + ['削除されます。', '保存されます。'][self.config.save_videos])
            if self.config.status_message:
                log.info("  プレイ中等の表示: " + self.config.status_message)
            log.info("  現在の曲のファイルへの書き込み: " + ['無効', '有効'][self.config.write_current_song])
            log.info("  追加した人の強制スキップ: " + ['無効', '有効'][self.config.allow_author_skip])
            log.info("  埋め込み: " + ['無効', '有効'][self.config.embeds])
            log.info("  Spotify との連携: " + ['無効', '有効'][self.config._spotify])
            log.info("  レガシースキップ: " + ['無効', '有効'][self.config.legacy_skip])
            log.info("  管理者がいない場合、サーバーから退出: " + ['無効', '有効'][self.config.leavenonowners])

        print(flush=True)

        await self.update_now_playing_status()

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        await self._join_startup_channels(self.autojoin_channels, autosummon=self.config.auto_summon)

        # we do this after the config stuff because it's a lot easier to notice here
        if self.config.missing_keys:
            log.warning('あなたの設定ファイルは不完全です。最近アップデートした場合は '
                        '`example_options.ini` を確認して、追加された項目を設定してください。 '
                        '欠けている項目: {0}'.format(self.config.missing_keys))
            print(flush=True)

        # t-t-th-th-that's all folks!

    def _gen_embed(self):
        """Provides a basic template for embeds"""
        e = discord.Embed()
        e.colour = 7506394
        e.set_footer(text='warota bot (バージョン {})'.format(BOTVERSION), icon_url='https://i.imgur.com/G3exlrI.jpg')
        e.set_author(name=self.user.name, url='https://yude.moe/bot', icon_url=self.user.avatar_url)
        return e

    async def cmd_resetplaylist(self, player, channel):
        """
        使用法:
            {command_prefix}resetplaylist

        自動プレイリストに追加されている項目をリセットします。
        """
        player.autoplaylist = list(set(self.autoplaylist))
        return Response(self.str.get('cmd-resetplaylist-response', '\N{OK HAND SIGN}'), delete_after=15)

    async def cmd_help(self, message, channel, command=None):
        """
        使用法:
            {command_prefix}help [command]

        ヘルプを表示します。
        何かコマンドが指定された場合は、それについての詳細を表示します。
        """
        self.commands = []
        self.is_all = False
        prefix = self.config.command_prefix

        if command:
            if command.lower() == 'all':
                self.is_all = True
                await self.gen_cmd_list(message, list_all_cmds=True)

            else:
                cmd = getattr(self, 'cmd_' + command, None)
                if cmd and not hasattr(cmd, 'dev_cmd'):
                    return Response(
                        "```\n{}```".format(
                            dedent(cmd.__doc__)
                        ).format(command_prefix=self.config.command_prefix),
                        delete_after=60
                    )
                else:
                    raise exceptions.CommandError(self.str.get('cmd-help-invalid', "No such command"), expire_in=10)

        elif message.author.id == self.config.owner_id:
            await self.gen_cmd_list(message, list_all_cmds=True)

        else:
            await self.gen_cmd_list(message)

        desc = '```\n' + ', '.join(self.commands) + '\n```\n' + self.str.get(
            'cmd-help-response', '詳しくは https://yude.moe/bot をご覧ください。\n'
                                 '`{}help (コマンド)` でコマンド毎の詳しい説明を表示します。').format(prefix)
        if not self.is_all:
            desc += self.str.get('cmd-help-all', '\nあなたが使用できるコマンドのみ表示しています。すべてのコマンドを表示するには、`{}help all` を実行してください。').format(prefix)

        return Response(desc, reply=True, delete_after=60)

    async def cmd_blacklist(self, message, user_mentions, option, something):
        """
        使用法:
            {command_prefix}blacklist [ + | - | add | remove ] @UserName [@UserName2 ...]

        ブラックリストからユーザーを追加したり、削除したりします。
        ブラックリストに追加されたユーザーはボットを操作することができなくなります。
        """

        if not user_mentions:
            raise exceptions.CommandError("誰も登録されていません。", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                self.str.get('cmd-blacklist-invalid', '不正なオプション "{0}" が指定されました。+、 -、 add、remove のどれかを使用してください。').format(option), expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                print("[Commands:Blacklist] 管理者をブラックリストへ追加することはできません。")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                self.str.get('cmd-blacklist-added', '{0} 人のユーザーがブラックリストへ登録されています。').format(len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response(self.str.get('cmd-blacklist-none', '誰も登録されていません。'), reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    self.str.get('cmd-blacklist-removed', '{0} 人のユーザーがブラックリストから削除されました。').format(old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        使用法:
            {command_prefix}id [@user]

        自分 または 他のユーザー の内部IDを表示します。
        """
        if not user_mentions:
            return Response(self.str.get('cmd-id-self', 'あなたのIDは `{0}` です。').format(author.id), reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response(self.str.get('cmd-id-other', '**{0}** のIDは `{1}` です。').format(usr.name, usr.id), reply=True, delete_after=35)

    async def cmd_save(self, player, url=None):
        """
        使用法:
            {command_prefix}save [url]

        プレイリストに項目を保存します。何もURLが指定されなかった場合は、再生中の曲を保存します。
        """
        if url or (player.current_entry and not isinstance(player.current_entry, StreamPlaylistEntry)):
            if not url:
                url = player.current_entry.url

            if url not in self.autoplaylist:
                self.autoplaylist.append(url)
                write_file(self.config.auto_playlist_file, self.autoplaylist)
                log.debug("Appended {} to autoplaylist".format(url))
                return Response(self.str.get('cmd-save-success', 'Added <{0}> to the autoplaylist.').format(url))
            else:
                raise exceptions.CommandError(self.str.get('cmd-save-exists', 'This song is already in the autoplaylist.'))
        else:
            raise exceptions.CommandError(self.str.get('cmd-save-invalid', 'There is no valid song playing.'))

    @owner_only
    async def cmd_joinserver(self, message, server_link=None):
        """
        使用法:
            {command_prefix}joinserver invite_link

        ボットをサーバーへ追加するためのURLを表示します。
        """

        url = await self.generate_invite_link()
        return Response(
            self.str.get('cmd-joinserver-response', "Click here to add me to a server: \n{}").format(url),
            reply=True, delete_after=30
        )

    async def cmd_karaoke(self, player, channel, author):
        """
        使用法:
            {command_prefix}karaoke

        カラオケモードを有効にします。これが有効になっている間は、限られたユーザーのみが項目をキューへ追加できます。
        """
        player.karaoke_mode = not player.karaoke_mode
        return Response("\N{OK HAND SIGN} カラオケモードが" + ['無効になりました。', '有効になりました。'][player.karaoke_mode], delete_after=15)

    async def _do_playlist_checks(self, permissions, player, author, testobj):
        num_songs = sum(1 for _ in testobj)

        # I have to do exe extra checks anyways because you can request an arbitrary number of search results
        if not permissions.allow_playlists and num_songs > 1:
            raise exceptions.PermissionsError(self.str.get('playlists-noperms', "プレイリストを追加するための権限がありません。"), expire_in=30)

        if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
            raise exceptions.PermissionsError(
                self.str.get('playlists-big', "Playlist has too many entries ({0} > {1})").format(num_songs, permissions.max_playlist_length),
                expire_in=30
            )

        # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
        if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('playlists-limit', "キューへ追加されている曲が多すぎます。({0} + {1} > {2})").format(
                    num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                expire_in=30
            )
        return True

    async def cmd_play(self, message, player, channel, author, permissions, leftover_args, song_url):
        """
        使用法:
            {command_prefix}play （動画のURL）
            {command_prefix}play （検索キーワード）
            {command_prefix}play （Spotify URI）

        動画をキューへ追加します。キーワードが指定された場合、検索結果の一番最初に出てくるものが再生されます。

        設定ファイル中で有効になっている場合、Spotify URIもサポートしますが、それはYouTubeから検索することに留まります。
        Spotify の素晴らしい音質で直接ストリーミングすることはできません。
        """

        song_url = song_url.strip('<>')

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])
        leftover_args = None  # prevent some crazy shit happening down the line

        # Make sure forward slashes work properly in search queries
        linksRegex = '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
        pattern = re.compile(linksRegex)
        matchUrl = pattern.match(song_url)
        song_url = song_url.replace('/', '%2F') if matchUrl is None else song_url

        # Rewrite YouTube playlist URLs if the wrong URL type is given
        playlistRegex = r'watch\?v=.+&(list=[^&]+)'
        matches = re.search(playlistRegex, song_url)
        groups = matches.groups() if matches is not None else []
        song_url = "https://www.youtube.com/playlist?" + groups[0] if len(groups) > 0 else song_url

        if self.config._spotify:
            if 'open.spotify.com' in song_url:
                song_url = 'spotify:' + re.sub('(http[s]?:\/\/)?(open.spotify.com)\/', '', song_url).replace('/', ':')
            if song_url.startswith('spotify:'):
                parts = song_url.split(":")
                try:
                    if 'track' in parts:
                        res = await self.spotify.get_track(parts[-1])
                        song_url = res['artists'][0]['name'] + ' ' + res['name'] 

                    elif 'album' in parts:
                        res = await self.spotify.get_album(parts[-1])
                        await self._do_playlist_checks(permissions, player, author, res['tracks']['items'])
                        procmesg = await self.safe_send_message(channel, self.str.get('cmd-play-spotify-album-process', 'Processing album `{0}` (`{1}`)').format(res['name'], song_url))
                        for i in res['tracks']['items']:
                            song_url = i['name'] + ' ' + i['artists'][0]['name']
                            log.debug('Processing {0}'.format(song_url))
                            await self.cmd_play(message, player, channel, author, permissions, leftover_args, song_url)
                        await self.safe_delete_message(procmesg)
                        return Response(self.str.get('cmd-play-spotify-album-queued', "Enqueued `{0}` with **{1}** songs.").format(res['name'], len(res['tracks']['items'])))
                    
                    elif 'playlist' in parts:
                        res = []
                        r = await self.spotify.get_playlist_tracks(parts[-1])
                        while True:
                            res.extend(r['items'])
                            if r['next'] is not None:
                                r = await self.spotify.make_spotify_req(r['next'])
                                continue
                            else:
                                break
                        await self._do_playlist_checks(permissions, player, author, res)
                        procmesg = await self.safe_send_message(channel, self.str.get('cmd-play-spotify-playlist-process', 'Processing playlist `{0}` (`{1}`)').format(parts[-1], song_url))
                        for i in res:
                            song_url = i['track']['name'] + ' ' + i['track']['artists'][0]['name']
                            log.debug('Processing {0}'.format(song_url))
                            await self.cmd_play(message, player, channel, author, permissions, leftover_args, song_url)
                        await self.safe_delete_message(procmesg)
                        return Response(self.str.get('cmd-play-spotify-playlist-queued', "Enqueued `{0}` with **{1}** songs.").format(parts[-1], len(res)))

                    else:
                        raise exceptions.CommandError(self.str.get('cmd-play-spotify-unsupported', 'That is not a supported Spotify URI.'), expire_in=30)
                except exceptions.SpotifyError:
                    raise exceptions.CommandError(self.str.get('cmd-play-spotify-invalid', 'You either provided an invalid URI, or there was a problem.'))

        async with self.aiolocks[_func_() + ':' + str(author.id)]:
            if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
                raise exceptions.PermissionsError(
                    self.str.get('cmd-play-limit', "これ以上項目を追加できません。 ({0})").format(permissions.max_songs), expire_in=30
                )

            if player.karaoke_mode and not permissions.bypass_karaoke_mode:
                raise exceptions.PermissionsError(
                    self.str.get('karaoke-enabled', "カラオケモードが有効になっています。これを無効化してから試してください。"), expire_in=30
                )

            try:
                info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            except Exception as e:
                if 'unknown url type' in str(e):
                    song_url = song_url.replace(':', '')  # it's probably not actually an extractor
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                else:
                    raise exceptions.CommandError(e, expire_in=30)

            if not info:
                raise exceptions.CommandError(
                    self.str.get('cmd-play-noinfo', "その動画は再生できません。`{0}stream` を試してみてください。").format(self.config.command_prefix),
                    expire_in=30
                )

            log.debug(info)

            if info.get('extractor', '') not in permissions.extractors and permissions.extractors:
                raise exceptions.PermissionsError(
                    self.str.get('cmd-play-badextractor', "このサービスより再生する権限がありません。"), expire_in=30
                )

            # abstract the search handling away from the user
            # our ytdl options allow us to use search strings as input urls
            if info.get('url', '').startswith('ytsearch'):
                # print("[Command:play] Searching for \"%s\"" % song_url)
                info = await self.downloader.extract_info(
                    player.playlist.loop,
                    song_url,
                    download=False,
                    process=True,    # ASYNC LAMBDAS WHEN
                    on_error=lambda e: asyncio.ensure_future(
                        self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                    retry_on_error=True
                )

                if not info:
                    raise exceptions.CommandError(
                        self.str.get('cmd-play-nodata', "検索中に何かエラーが発生しました。(youtube-dl が反応しませんでした。）"
                                                        "ボットを再起動する必要があるかもしれません。"), expire_in=30
                    )

                if not all(info.get('entries', [])):
                    # empty list, no data
                    log.debug("何もないリストを受け取りました。")
                    return

                # TODO: handle 'webpage_url' being 'ytsearch:...' or extractor type
                song_url = info['entries'][0]['webpage_url']
                info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
                # But this is probably fine

            # TODO: Possibly add another check here to see about things like the bandcamp issue
            # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

            if 'entries' in info:
                await self._do_playlist_checks(permissions, player, author, info['entries'])

                num_songs = sum(1 for _ in info['entries'])

                if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                    try:
                        return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                    except exceptions.CommandError:
                        raise
                    except Exception as e:
                        log.error("プレイリストを追加できませんでした。", exc_info=True)
                        raise exceptions.CommandError(self.str.get('cmd-play-playlist-error', "プレイリストを追加できませんでした:\n`{0}`").format(e), expire_in=30)

                t0 = time.time()

                # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
                # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
                # I don't think we can hook into it anyways, so this will have to do.
                # It would probably be a thread to check a few playlists and get the speed from that
                # Different playlists might download at different speeds though
                wait_per_song = 1.2

                procmesg = await self.safe_send_message(
                    channel,
                    self.str.get('cmd-play-playlist-gathering-1', '{0} 個の項目を処理しています。{1}').format(
                        num_songs,
                        self.str.get('cmd-play-playlist-gathering-2', ' 残り時間: 後 {0} 秒').format(fixg(
                            num_songs * wait_per_song)) if num_songs >= 10 else '.'))

                # We don't have a pretty way of doing this yet.  We need either a loop
                # that sends these every 10 seconds or a nice context manager.
                await self.send_typing(channel)

                # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
                #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

                entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

                tnow = time.time()
                ttime = tnow - t0
                listlen = len(entry_list)
                drop_count = 0

                if permissions.max_song_length:
                    for e in entry_list.copy():
                        if e.duration > permissions.max_song_length:
                            player.playlist.entries.remove(e)
                            entry_list.remove(e)
                            drop_count += 1
                            # Im pretty sure there's no situation where this would ever break
                            # Unless the first entry starts being played, which would make this a race condition
                    if drop_count:
                        print("%s 個の項目を削除しました。" % drop_count)

                log.info("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
                    listlen,
                    fixg(ttime),
                    ttime / listlen if listlen else 0,
                    ttime / listlen - wait_per_song if listlen - wait_per_song else 0,
                    fixg(wait_per_song * num_songs))
                )

                await self.safe_delete_message(procmesg)

                if not listlen - drop_count:
                    raise exceptions.CommandError(
                        self.str.get('cmd-play-playlist-maxduration', "何も追加されませんでした。すべての項目が長すぎます。 (%ss)") % permissions.max_song_length,
                        expire_in=30
                    )

                reply_text = self.str.get('cmd-play-playlist-reply', "**%s**つの項目を追加しました。順序: %s")
                btext = str(listlen - drop_count)

            else:
                if info.get('extractor', '').startswith('youtube:playlist'):
                    try:
                        info = await self.downloader.extract_info(player.playlist.loop, 'https://www.youtube.com/watch?v=%s' % info.get('url', ''), download=False, process=False)
                    except Exception as e:
                        raise exceptions.CommandError(e, expire_in=30)

                if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                    raise exceptions.PermissionsError(
                        self.str.get('cmd-play-song-limit', "動画が長すぎます。 ({0} > {1})").format(info['duration'], permissions.max_song_length),
                        expire_in=30
                    )

                try:
                    entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

                except exceptions.WrongEntryTypeError as e:
                    if e.use_url == song_url:
                        log.warning("Determined incorrect entry type, but suggested url is the same.  Help.")

                    log.debug("Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                    log.debug("Using \"%s\" instead" % e.use_url)

                    return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)

                reply_text = self.str.get('cmd-play-song-reply', "`%s` をキューに追加しました。順番: %s")
                btext = entry.title


            if position == 1 and player.is_stopped:
                position = self.str.get('cmd-play-next', '次')
                reply_text %= (btext, position)

            else:
                try:
                    time_until = await player.playlist.estimate_time_until(position, player)
                    reply_text += self.str.get('cmd-play-eta', ' - 再生されるまで後 %s')
                except:
                    traceback.print_exc()
                    time_until = ''

                reply_text %= (btext, position, ftimedelta(time_until))

        return Response(reply_text, delete_after=30)

    async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
        """
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError(self.str.get('cmd-play-playlist-invalid', "そのプレイリストは再生できません。"))

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, self.str.get('cmd-play-playlist-process', "{0}個の項目を処理しています...").format(num_songs))  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError(self.str.get('cmd-play-playlist-queueerror', 'Error handling playlist {0} queuing.').format(playlist_url), expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise exceptions.CommandError(self.str.get('cmd-play-playlist-queueerror', 'プレイリスト {0} を追加中に何か問題が発生しました。').format(playlist_url), expire_in=30)


        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                log.debug("%s個の項目を削除しました。" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.guild]['last_np_msg'])
                self.server_specific_data[channel.guild]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        log.info("{}/{} の項目を {} 秒 ({:.2f}曲毎秒) で処理しました。 {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            fixg(ttime),
            ttime / num_songs if num_songs else 0,
            ttime / num_songs - wait_per_song if num_songs - wait_per_song else 0,
            fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = self.str.get('cmd-play-playlist-maxduration', "何も追加されませんでした。すべての項目が長すぎます。 (%ss)") % permissions.max_song_length
            if skipped:
                basetext += self.str.get('cmd-play-playlist-skipped', "\n更に、次の項目も長過ぎたためスキップされました。これはひどい。")

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response(self.str.get('cmd-play-playlist-reply-secs', "{0} 個の項目が追加されました。（{1}秒後に再生されます）").format(
            songs_added, fixg(ttime, 1)), delete_after=30)

    async def cmd_stream(self, player, channel, author, permissions, song_url):
        """
        使用法:
            {command_prefix}stream song_link

        Enqueue a media stream.
        This could mean an actual stream like Twitch or shoutcast, or simply streaming
        media without predownloading it.  Note: FFmpeg is notoriously bad at handling
        streams, especially on poor connections.  You have been warned.
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('cmd-stream-limit', "これ以上項目を追加できません。 ({0})").format(permissions.max_songs), expire_in=30
            )

        if player.karaoke_mode and not permissions.bypass_karaoke_mode:
            raise exceptions.PermissionsError(
                self.str.get('karaoke-enabled', "カラオケモードが有効になっています。これを無効化してから試してください。"), expire_in=30
            )

        await self.send_typing(channel)
        await player.playlist.add_stream_entry(song_url, channel=channel, author=author)

        return Response(self.str.get('cmd-stream-success', "ストリーミングしています。"), delete_after=6)

    async def cmd_search(self, message, player, channel, author, permissions, leftover_args):
        """
        使用法:
            {command_prefix}search [サービス] [件数] 検索キーワード

        動画共有サービス等から検索して、追加できます。
        - サービス: 次のサービスが利用できます。:
            - youtube (yt) (既定)
            - soundcloud (sc)
            - yahoo (yh)
        - 件数: 表示する検索結果の件数を指定します。
          - 何も指定されなければ、3つまで表示されます。
          - 検索キーワードが数字から始まる場合、クオーテーションで囲う必要があります。
            - 例: {command_prefix}search 2 "I ran seagulls"
        コマンドを実行した人のみが検索結果を操作できます。
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise exceptions.PermissionsError(
                self.str.get('cmd-search-limit', "これ以上項目を追加できません。 ({0})").format(permissions.max_songs),
                expire_in=30
            )

        if player.karaoke_mode and not permissions.bypass_karaoke_mode:
            raise exceptions.PermissionsError(
                self.str.get('karaoke-enabled', "カラオケモードが有効になっています。これを無効化してから試してください。"), expire_in=30
            )

        def argcheck():
            if not leftover_args:
                # noinspection PyUnresolvedReferences
                raise exceptions.CommandError(
                    self.str.get('cmd-search-noquery', "検索キーワードを指定してください。\n%s") % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError(self.str.get('cmd-search-noquote', "検索キーワードを正しく入力してください。"), expire_in=30)

        service = 'youtube'
        items_requested = 3
        max_items = permissions.max_search_items
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError(self.str.get('cmd-search-searchlimit', "You cannot search for more than %s videos") % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.safe_send_message(channel, self.str.get('cmd-search-searching', "動画を検索しています..."))
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response(self.str.get('cmd-search-none', "何も見つかりませんでした。"), delete_after=30)

        for e in info['entries']:
            result_message = await self.safe_send_message(channel, self.str.get('cmd-search-result', "検索結果 {0}/{1}: {2}").format(
                info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

            def check(reaction, user):
                return user == message.author and reaction.message.id == result_message.id  # why can't these objs be compared directly?

            reactions = ['\u2705', '\U0001F6AB', '\U0001F3C1']
            for r in reactions:
                await result_message.add_reaction(r)

            try:
                reaction, user = await self.wait_for('reaction_add', timeout=30.0, check=check)
            except asyncio.TimeoutError:
                await self.safe_delete_message(result_message)
                return

            if str(reaction.emoji) == '\u2705':  # check
                await self.safe_delete_message(result_message)
                await self.cmd_play(message, player, channel, author, permissions, [], e['webpage_url'])
                return Response(self.str.get('cmd-search-accept', "分かりました。もうすぐ再生されます。"), delete_after=30)
            elif str(reaction.emoji) == '\U0001F6AB':  # cross
                await self.safe_delete_message(result_message)
                continue
            else:
                await self.safe_delete_message(result_message)
                break

        return Response(self.str.get('cmd-search-decline', "(´・ω・｀) そっか..."), delete_after=30)

    async def cmd_np(self, player, channel, guild, message):
        """
        使用法:
            {command_prefix}np

        Displays the current song in chat.
        """

        if player.current_entry:
            if self.server_specific_data[guild]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[guild]['last_np_msg'])
                self.server_specific_data[guild]['last_np_msg'] = None

            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))

            streaming = isinstance(player.current_entry, StreamPlaylistEntry)
            prog_str = ('`[{progress}]`' if streaming else '`[{progress}/{total}]`').format(
                progress=song_progress, total=song_total
            )
            prog_bar_str = ''

            # percentage shows how much of the current song has already been played
            percentage = 0.0
            if player.current_entry.duration > 0:
                percentage = player.progress / player.current_entry.duration

            # create the actual bar
            progress_bar_length = 30
            for i in range(progress_bar_length):
                if (percentage < 1 / progress_bar_length * i):
                    prog_bar_str += '□'
                else:
                    prog_bar_str += '■'

            action_text = self.str.get('cmd-np-action-streaming', 'ストリーミング中') if streaming else self.str.get('cmd-np-action-playing', '再生中')

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = self.str.get('cmd-np-reply-author', "{action}: **{title}** (by {author})\n　　{progress_bar} {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>").format(
                    action=action_text,
                    title=player.current_entry.title,
                    author=player.current_entry.meta['author'].name,
                    progress_bar=prog_bar_str,
                    progress=prog_str,
                    url=player.current_entry.url
                )
            else:

                np_text = self.str.get('cmd-np-reply-noauthor', "{action}: **{title}**\n　　{progress_bar} {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>").format(

                    action=action_text,
                    title=player.current_entry.title,
                    progress_bar=prog_bar_str,
                    progress=prog_str,
                    url=player.current_entry.url
                )

            self.server_specific_data[guild]['last_np_msg'] = await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                self.str.get('cmd-np-none', 'キューに何も追加されていません。`{0}play` を使用して、何か曲を追加してください。') .format(self.config.command_prefix),
                delete_after=30
            )

    async def cmd_summon(self, channel, guild, author, voice_channel):
        """
        使用法:
            {command_prefix}summon

        Call the bot to the summoner's voice channel.
        """

        if not author.voice:
            raise exceptions.CommandError(self.str.get('cmd-summon-novc', '音声チャンネルに参加していません。`.summon` を使用して、参加させてください。'))

        voice_client = self.voice_client_in(guild)
        if voice_client and guild == author.voice.channel.guild:
            await voice_client.move_to(author.voice.channel)
        else:
            # move to _verify_vc_perms?
            chperms = author.voice.channel.permissions_for(guild.me)

            if not chperms.connect:
                log.warning("'{0}'に接続できません。参加する権限がありません。".format(author.voice.channel.name))
                raise exceptions.CommandError(
                    self.str.get('cmd-summon-noperms-connect', "'{0}'に接続できません。参加する権限がありません。").format(author.voice.channel.name),
                    expire_in=25
                )

            elif not chperms.speak:
                log.warning("'{0}'に接続できません。音声を発信する権限がありません。".format(author.voice.channel.name))
                raise exceptions.CommandError(
                    self.str.get('cmd-summon-noperms-speak', "'{0}'に接続できません。音声を発信する権限がありません。").format(author.voice.channel.name),
                    expire_in=25
                )

            player = await self.get_player(author.voice.channel, create=True, deserialize=self.config.persistent_queue)

            if player.is_stopped:
                player.play()

            if self.config.auto_playlist:
                await self.on_player_finished_playing(player)

        log.info("Joining {0.guild.name}/{0.name}".format(author.voice.channel))

        return Response(self.str.get('cmd-summon-reply', '`{0.name}` へ接続しました。').format(author.voice.channel))

    async def cmd_pause(self, player):
        """
        使用法:
            {command_prefix}pause

        Pauses playback of the current song.
        """

        if player.is_playing:
            player.pause()
            return Response(self.str.get('cmd-pause-reply', '`{0.name}` で再生されていた曲を一時停止しました。').format(player.voice_client.channel))

        else:
            raise exceptions.CommandError(self.str.get('cmd-pause-none', '何も再生していません。'), expire_in=30)

    async def cmd_resume(self, player):
        """
        使用法:
            {command_prefix}resume

        Resumes playback of a paused song.
        """

        if player.is_paused:
            player.resume()
            return Response(self.str.get('cmd-resume-reply', '`{0.name}` で一時停止されていた曲をリジュームしました。').format(player.voice_client.channel), delete_after=15)

        else:
            raise exceptions.CommandError(self.str.get('cmd-resume-none', '一時停止していません。'), expire_in=30)

    async def cmd_shuffle(self, channel, player):
        """
        使用法:
            {command_prefix}shuffle

        Shuffles the server's queue.
        """

        player.playlist.shuffle()

        cards = ['\N{BLACK SPADE SUIT}', '\N{BLACK CLUB SUIT}', '\N{BLACK HEART SUIT}', '\N{BLACK DIAMOND SUIT}']
        random.shuffle(cards)

        hand = await self.safe_send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            random.shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response(self.str.get('cmd-shuffle-reply', "`{0}` のキューをシャッフルしました。").format(player.voice_client.channel.guild), delete_after=15)

    async def cmd_clear(self, player, author):
        """
        使用法:
            {command_prefix}clear

        Clears the playlist.
        """

        player.playlist.clear()
        return Response(self.str.get('cmd-clear-reply', "`{0}` のキューを綺麗さっぱり消しました。").format(player.voice_client.channel.guild), delete_after=20)

    async def cmd_remove(self, user_mentions, message, author, permissions, channel, player, index=None):
        """
        使用法:
            {command_prefix}remove [# in queue]

        Removes queued songs. If a number is specified, removes that song in the queue, otherwise removes the most recently queued song.
        """

        if not player.playlist.entries:
            raise exceptions.CommandError(self.str.get('cmd-remove-none', "消すものが何もありません。"), expire_in=20)

        if user_mentions:
            for user in user_mentions:
                if permissions.remove or author == user:
                    try:
                        entry_indexes = [e for e in player.playlist.entries if e.meta.get('author', None) == user]
                        for entry in entry_indexes:
                            player.playlist.entries.remove(entry)
                        entry_text = '%s ' % len(entry_indexes) + 'item'
                        if len(entry_indexes) > 1:
                            entry_text += 's'
                        return Response(self.str.get('cmd-remove-reply', "`{0}` （by `{1}`） を削除しました。").format(entry_text, user.name).strip())

                    except ValueError:
                        raise exceptions.CommandError(self.str.get('cmd-remove-missing', "`%s` によって追加された項目はありません。") % user.name, expire_in=20)

                raise exceptions.PermissionsError(
                    self.str.get('cmd-remove-noperms', "キューからその項目を削除できません。それを追加した本人か、または権限を持っているユーザーのみが削除できます。"), expire_in=20)

        if not index:
            index = len(player.playlist.entries)

        try:
            index = int(index)
        except (TypeError, ValueError):
            raise exceptions.CommandError(self.str.get('cmd-remove-invalid', "無効な数字です。 {}queue を使用して、指定するべき数字を確認してください。").format(self.config.command_prefix), expire_in=20)

        if index > len(player.playlist.entries):
            raise exceptions.CommandError(self.str.get('cmd-remove-invalid', "無効な数字です。 {}queue を使用して、指定するべき数字を確認してください。").format(self.config.command_prefix), expire_in=20)

        if permissions.remove or author == player.playlist.get_entry_at_index(index - 1).meta.get('author', None):
            entry = player.playlist.delete_entry_at_index((index - 1))
            await self._manual_delete_check(message)
            if entry.meta.get('channel', False) and entry.meta.get('author', False):
                return Response(self.str.get('cmd-remove-reply-author', "`{0}` （by `{1}`） を削除しました。").format(entry.title, entry.meta['author'].name).strip())
            else:
                return Response(self.str.get('cmd-remove-reply-noauthor', "`{0}` を削除しました。").format(entry.title).strip())
        else:
            raise exceptions.PermissionsError(
                self.str.get('cmd-remove-noperms', "キューからその項目を削除できません。それを追加した本人か、または権限を持っているユーザーのみが削除できます。"), expire_in=20
            )

    async def cmd_skip(self, player, channel, author, message, permissions, voice_channel, param=''):
        """
        使用法:
            {command_prefix}skip [force/f]

        再生中の曲をスキップします。
        権限を持っている一部のユーザーか、管理者のみが強制的にスキップさせることができます。
        """

        if player.is_stopped:
            raise exceptions.CommandError(self.str.get('cmd-skip-none', "スキップできません。何も再生していません。"), expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    return Response(self.str.get('cmd-skip-dl', "次の項目 (`%s`) はダウンロード中です。しばらくお待ちください。") % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("次の曲はもうすぐ再生されます。しばらくお待ちください。")
                else:
                    print("通常より処理に時間がかかっています。"
                          "しばらく待っても改善しない場合、ボットを再起動させてください。")
            else:
                print("何か問題が発生しました。"
                      "しばらく待っても改善しない場合、ボットを再起動させてください。")
        
        current_entry = player.current_entry

        if (param.lower() in ['force', 'f']) or self.config.legacy_skip:
            if permissions.instaskip \
                or (self.config.allow_author_skip and author == player.current_entry.meta.get('author', None)):

                player.skip()  # TODO: check autopause stuff here
                await self._manual_delete_check(message)
                return Response(self.str.get('cmd-skip-force', '`{}` をスキップさせました。').format(current_entry.title), reply=True, delete_after=30)
            else:
                raise exceptions.PermissionsError(self.str.get('cmd-skip-force-noperms', '強制的にスキップさせる権限をあなたは持っていません。'), expire_in=30)

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.members if not (
            m.voice.deaf or m.voice.self_deaf or m == self.user))
        if num_voice == 0: num_voice = 1 # incase all users are deafened, to avoid divison by zero

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(
            self.config.skips_required,
            math.ceil(self.config.skip_ratio_required / (1 / num_voice))  # Number of skips from config ratio
        ) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            # @TheerapakG: Check for pausing state in the player.py make more sense
            return Response(
                self.str.get('cmd-skip-reply-skipped-1', '`{0}` に対するスキップが認められました。{1}').format(
                    current_entry.title,
                    self.str.get('cmd-skip-reply-skipped-2', '\n次の曲が再生されます。') if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                self.str.get('cmd-skip-reply-voted-1', 'あなたは `{0}` に対するスキップに投票しました。\n**{1}** さらに{2}人のユーザーが必要です。').format(
                    current_entry.title,
                    skips_remaining,
                    self.str.get('cmd-skip-reply-voted-2', '投票者: ') if skips_remaining == 1 else self.str.get('cmd-skip-reply-voted-3', '投票者: ')
                ),
                reply=True,
                delete_after=20
            )

    async def cmd_volume(self, message, player, new_volume=None):
        """
        使用法:
            {command_prefix}volume (+/-)[値]

        1 から 100 の間で音量を指定できます。また、**+**や**-**を指定することで、現在の値から
        """

        if not new_volume:
            return Response(self.str.get('cmd-volume-current', '現在の音量: `%s%%`') % int(player.volume * 100), reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError(self.str.get('cmd-volume-invalid', '`{0}` は正しい数字ではありません。').format(new_volume), expire_in=20)

        vol_change = None
        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response(self.str.get('cmd-volume-reply', '音量を **%d** から **%d** へ変更しました。') % (old_volume, new_volume), reply=True, delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    self.str.get('cmd-volume-unreasonable-relative', 'ピピッ... あり得ない音量の変化を観測しました。: {}{:+} -> {}% {} から {:+} の間で指定してください。').format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
            else:
                raise exceptions.CommandError(
                    self.str.get('cmd-volume-unreasonable-absolute', 'ピピッ... あり得ない音量の変化を観測しました: {}% 1から100の間で指定してください。').format(new_volume), expire_in=20)

    @owner_only
    async def cmd_option(self, player, option, value):
        """
        使用法:
            {command_prefix}option [オプション名] [on/y/enabled/off/n/disabled]

        再起動せずに設定を変更できます。再起動するまでは変更が保持されますが、もしした場合は設定ファイルに上書きされます。

        有効なオプション名:
            autoplaylist, save_videos, now_playing_mentions, auto_playlist_random, auto_pause,
            delete_messages, delete_invoking, write_current_song
        """

        option = option.lower()
        value = value.lower()
        bool_y = ['on', 'y', 'enabled']
        bool_n = ['off', 'n', 'disabled']
        generic = ['save_videos', 'now_playing_mentions', 'auto_playlist_random',
                   'auto_pause', 'delete_messages', 'delete_invoking',
                   'write_current_song']  # these need to match attribute names in the Config class
        if option in ['autoplaylist', 'auto_playlist']:
            if value in bool_y:
                if self.config.auto_playlist:
                    raise exceptions.CommandError(self.str.get('cmd-option-autoplaylist-enabled', '自動プレイリストはすでに**有効**になっています。'))
                else:
                    if not self.autoplaylist:
                        raise exceptions.CommandError(self.str.get('cmd-option-autoplaylist-none', 'プレイリストファイルに何も追加されていません。'))
                    self.config.auto_playlist = True
                    await self.on_player_finished_playing(player)
            elif value in bool_n:
                if not self.config.auto_playlist:
                    raise exceptions.CommandError(self.str.get('cmd-option-autoplaylist-disabled', '自動プレイリストはすでに**無効**になっています。'))
                else:
                    self.config.auto_playlist = False
            else:
                raise exceptions.CommandError(self.str.get('cmd-option-invalid-value', '指定されたパラメータは無効です。'))
            return Response("自動プレイリストが" + ['無効化されました。', '有効化されました。'][self.config.auto_playlist] + '.')
        else:
            is_generic = [o for o in generic if o == option]  # check if it is a generic bool option
            if is_generic and (value in bool_y or value in bool_n):
                name = is_generic[0]
                log.debug('Setting attribute {0}'.format(name))
                setattr(self.config, name, True if value in bool_y else False)  # this is scary but should work
                attr = getattr(self.config, name)
                res = "オプション「{0}」".format(option) + ['は無効化されました。', '有効化されました。'][attr] + '.'
                log.warning('オプションが次の理由により上書きされました: {0}'.format(res))
                return Response(res)
            else:
                raise exceptions.CommandError(self.str.get('cmd-option-invalid-param' ,'指定されたパラメータは無効です。'))

    async def cmd_queue(self, channel, player):
        """
        使用法:
            {command_prefix}queue

        キューを表示します。
        """

        lines = []
        unlisted = 0
        andmoretext = '* ... 更に%s件*' % ('x' * len(player.playlist.entries))

        if player.is_playing:
            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append(self.str.get('cmd-queue-playing-author', "再生中: `{0}` (by `{1}`) {2}\n").format(
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
            else:
                lines.append(self.str.get('cmd-queue-playing-noauthor', "再生中: `{0}` {1}\n").format(player.current_entry.title, prog_str))


        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = self.str.get('cmd-queue-entry-author', '{0} -- `{1}` by `{2}`').format(i, item.title, item.meta['author'].name).strip()
            else:
                nextline = self.str.get('cmd-queue-entry-noauthor', '{0} -- `{1}`').format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if (currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT) or (i > self.config.queue_length):
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append(self.str.get('cmd-queue-more', '\n... 更に%s件') % unlisted)

        if not lines:
            lines.append(
                self.str.get('cmd-queue-none', 'キューに何も追加されていません。`{}play` を使って、何か項目を追加してください。').format(self.config.command_prefix))

        message = '\n'.join(lines)
        return Response(message, delete_after=30)

    async def cmd_clean(self, message, channel, guild, author, search_range=50):
        """
        使用法:
            {command_prefix}clean [遡る件数]

        ボットが送信したメッセージを削除します。
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response(self.str.get('cmd-clean-invalid', "無効なパラメータです。"), reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(guild.me).manage_messages:
                deleted = await channel.purge(check=check, limit=search_range, before=message)
                return Response(self.str.get('cmd-clean-reply', '{0} 件のメッセージを削除しました。{1}').format(len(deleted), ' ' * bool(deleted)), delete_after=15)

    async def cmd_pldump(self, channel, author, song_url):
        """
        使用法:
            {command_prefix}pldump URL

        指定されたURLのプレイリストをダンプします。
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url.strip('<>'), download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError("入力されたURLから何も取得できませんでした。\n%s\n" % e, expire_in=25)

        if not info:
            raise exceptions.CommandError("何も取得できませんでした。何もデータがなかったので...", expire_in=25)

        if not info.get('entries', None):
            # TODO: Retarded playlist checking
            # set(url, webpageurl).difference(set(url))

            if info.get('url', None) != info.get('webpage_url', info.get('url', None)):
                raise exceptions.CommandError("無効なプレイリストファイルです。本当にそれはプレイリストファイルですか？", expire_in=25)
            else:
                return await self.cmd_pldump(channel, info.get(''))

        linegens = defaultdict(lambda: None, **{
            "youtube":    lambda d: 'https://www.youtube.com/watch?v=%s' % d['id'],
            "soundcloud": lambda d: d['url'],
            "bandcamp":   lambda d: d['url']
        })

        exfunc = linegens[info['extractor'].split(':')[0]]

        if not exfunc:
            raise exceptions.CommandError("URLが無効です。対応していない形式のようです。", expire_in=25)

        with BytesIO() as fcontent:
            for item in info['entries']:
                fcontent.write(exfunc(item).encode('utf8') + b'\n')

            fcontent.seek(0)
            await author.send("こちらが <%s> のダンプファイルです。" % song_url, file=discord.File(fcontent, filename='playlist.txt'))

        return Response("プレイリストファイルと共にコマンドを実行してください。", delete_after=20)

    async def cmd_listids(self, guild, author, leftover_args, cat='all'):
        """
        使用法:
            {command_prefix}listids [カテゴリー]

        様々なIDを表示します。カテゴリーには次のようなものが指定できます:
           all, users, roles, channels
        """

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "有効なカテゴリー: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['あなたのID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nユーザーID:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in guild.members]

            elif cur_cat == 'roles':
                data.append("\n権限ID:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in guild.roles]

            elif cur_cat == 'channels':
                data.append("\nテキストチャンネルのID:")
                tchans = [c for c in guild.channels if isinstance(c, discord.TextChannel)]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\n音声チャンネルのID:")
                vchans = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await author.send(file=discord.File(sdata, filename='%s-ids-%s.txt' % (guild.name.replace(' ', '_'), cat)))

        return Response("Sent a message with a list of IDs.", delete_after=20)


    async def cmd_perms(self, author, user_mentions, channel, guild, permissions):
        """
        使用法:
            {command_prefix}perms [@user]

        権限の一覧を表示します。
        """

        lines = ['Command permissions in %s\n' % guild.name, '```', '```']

        if user_mentions:
            user = user_mentions[0]
            permissions = self.permissions.for_user(user)

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.safe_send_message(author, '\n'.join(lines))
        return Response("\N{OPEN MAILBOX WITH RAISED FLAG}", delete_after=20)


    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        使用法:
            {command_prefix}setname (名前)

        ボットのユーザー名を変更します。
        注意: Discord によって、この動作は1時間に2回までに制限されています。
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.user.edit(username=name)

        except discord.HTTPException:
            raise exceptions.CommandError(
                "Failed to change name. Did you change names too many times?  "
                "Remember name changes are limited to twice per hour.")

        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("Set the bot's username to **{0}**".format(name), delete_after=20)

    async def cmd_setnick(self, guild, channel, leftover_args, nick):
        """
        使用法:
            {command_prefix}setnick nick

        ボットのニックネームを変更します。
        """

        if not channel.permissions_for(guild.me).change_nickname:
            raise exceptions.CommandError("Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await guild.me.edit(nick=nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("Set the bot's nickname to `{0}`".format(nick), delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        使用法:
            {command_prefix}setavatar [url]

        ボットのアイコンを変更します。
        ファイルを添付するか、URLを指定してください。
        """

        if message.attachments:
            thing = message.attachments[0].url
        elif url:
            thing = url.strip('<>')
        else:
            raise exceptions.CommandError("URLを貼り付けるか、ファイルを添付してください。", expire_in=20)

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.aiosession.get(thing, timeout=timeout) as res:
                await self.user.edit(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("アイコンを変更できませんでした。: {}".format(e), expire_in=20)

        return Response("ボットのアイコンを変更しました。", delete_after=20)


    async def cmd_disconnect(self, guild):
        """
        使用法:
            {command_prefix}disconnect
        
        ボットを接続している音声チャンネルから退出させます。
        """
        await self.disconnect_voice_client(guild)
        return Response("`{0.name}` から退出しました。".format(guild), delete_after=20)

    async def cmd_restart(self, channel):
        """
        使用法:
            {command_prefix}restart
        
        ボットを再起動します。前提ライブラリやボットのコードに変更が加えられた場合、正しく動作しない恐れがあります。
        """
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN} 再起動中です。前提ライブラリやボット自体のコードに変更を加えた場合は、ボットを一度シャットダウンさせてください。")

        player = self.get_player_in(channel.guild)
        if player and player.is_paused:
            player.resume()

        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal()

    async def cmd_shutdown(self, channel):
        """
        使用法:
            {command_prefix}shutdown
        
        ボットを音声チャンネルから切断させ、完全にシャットダウンさせます。
        """
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")
        
        player = self.get_player_in(channel.guild)
        if player and player.is_paused:
            player.resume()
        
        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal()

    async def cmd_leaveserver(self, val, leftover_args):
        """
        使用法:
            {command_prefix}leaveserver <サーバー名 / ID>

        指定したサーバーから退出します。名前を指定する場合、大文字小文字等が区別されます。
        """
        if leftover_args:
            val = ' '.join([val, *leftover_args])

        t = self.get_guild(val)
        if t is None:
            t = discord.utils.get(self.guilds, name=val)
            if t is None:
                raise exceptions.CommandError('`{0}` というIDまたは名前のサーバーは見当たりませんでした。'.format(val))
        await t.leave()
        return Response('Left the guild: `{0.name}` (Owner: `{0.owner.name}`, ID: `{0.id}`)'.format(t))

    @dev_only
    async def cmd_breakpoint(self, message):
        log.critical("Activating debug breakpoint")
        return

    @dev_only
    async def cmd_objgraph(self, channel, func='most_common_types()'):
        import objgraph

        await self.send_typing(channel)

        if func == 'growth':
            f = StringIO()
            objgraph.show_growth(limit=10, file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leaks':
            f = StringIO()
            objgraph.show_most_common_types(objects=objgraph.get_leaking_objects(), file=f)
            f.seek(0)
            data = f.read()
            f.close()

        elif func == 'leakstats':
            data = objgraph.typestats(objects=objgraph.get_leaking_objects())

        else:
            data = eval('objgraph.' + func)

        return Response(data, codeblock='py')

    @dev_only
    async def cmd_debug(self, message, _player, *, data):
        codeblock = "```py\n{}\n```"
        result = None

        if data.startswith('```') and data.endswith('```'):
            data = '\n'.join(data.rstrip('`\n').split('\n')[1:])

        code = data.strip('` \n')

        try:
            result = eval(code)
        except:
            try:
                exec(code)
            except Exception as e:
                traceback.print_exc(chain=False)
                return Response("{}: {}".format(type(e).__name__, e))

        if asyncio.iscoroutine(result):
            result = await result

        return Response(codeblock.format(result))

    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            return

        if message.author == self.user:
            log.warning("自分自身からのコマンドを無視します。 ({})".format(message.content))
            return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels:
            if self.config.unbound_servers:
                for channel in message.guild.channels:
                    if channel.id in self.config.bound_channels:
                        return
            else:
                return  # if I want to log this I just move it under the prefix check

        if (not isinstance(message.channel, discord.abc.GuildChannel)) and (not isinstance(message.channel, discord.abc.PrivateChannel)):
            return

        command, *args = message_content.split(' ')  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        command = command[len(self.config.command_prefix):].lower().strip()

        args = ' '.join(args).lstrip(' ').split(' ')

        handler = getattr(self, 'cmd_' + command, None)
        if not handler:
            return

        if isinstance(message.channel, discord.abc.PrivateChannel):
            if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                await self.safe_send_message(message.channel, 'You cannot use this bot in private messages.')
                return

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            log.warning("User blacklisted: {0.id}/{0!s} ({1})".format(message.author, command))
            return

        else:
            log.info("{0.id}/{0!s}: {1}".format(message.author, message_content.replace('\n', '\n... ')))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        sentmsg = response = None

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('guild', None):
                handler_kwargs['guild'] = message.guild

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('_player', None):
                handler_kwargs['_player'] = self.get_player_in(message.guild)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.guild.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.guild.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.guild.me.voice.channel if message.guild.me.voice else None

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):

                # parse (*args) as a list of args
                if param.kind == param.VAR_POSITIONAL:
                    handler_kwargs[key] = args
                    params.pop(key)
                    continue

                # parse (*, args) as args rejoined as a string
                # multiple of these arguments will have the same value
                if param.kind == param.KEYWORD_ONLY and param.default == param.empty:
                    handler_kwargs[key] = ' '.join(args)
                    params.pop(key)
                    continue

                doc_key = '[{}={}]'.format(key, param.default) if param.default is not param.empty else key
                args_expected.append(doc_key)

                # Ignore keyword args with default values when the command had no arguments
                if not args and param.default is not param.empty:
                    params.pop(key)
                    continue

                # Assign given values to positional arguments
                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

            # Invalid usage, return docstring
            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = dedent(docs)
                await self.safe_send_message(
                    message.channel,
                    '```\n{}\n```'.format(docs.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                if not isinstance(response.content, discord.Embed) and self.config.embeds:
                    content = self._gen_embed()
                    content.title = command
                    content.description = response.content
                else:
                    content = response.content

                if response.reply:
                    if isinstance(content, discord.Embed):
                        content.description = '{} {}'.format(message.author.mention, content.description if content.description is not discord.Embed.Empty else '')
                    else:
                        content = '{}: {}'.format(message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            log.error("Error in {0}: {1.__class__.__name__}: {1.message}".format(command, e), exc_info=True)

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            if self.config.embeds:
                content = self._gen_embed()
                content.add_field(name='Error', value=e.message, inline=False)
                content.colour = 13369344
            else:
                content = '```\n{}\n```'.format(e.message)

            await self.safe_send_message(
                message.channel,
                content,
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            log.error("Exception in on_message", exc_info=True)
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n{}\n```'.format(traceback.format_exc()))

        finally:
            if not sentmsg and not response and self.config.delete_invoking:
                await asyncio.sleep(5)
                await self.safe_delete_message(message, quiet=True)

    async def gen_cmd_list(self, message, list_all_cmds=False):
        for att in dir(self):
            # This will always return at least cmd_help, since they needed perms to run this command
            if att.startswith('cmd_') and not hasattr(getattr(self, att), 'dev_cmd'):
                user_permissions = self.permissions.for_user(message.author)
                command_name = att.replace('cmd_', '').lower()
                whitelist = user_permissions.command_whitelist
                blacklist = user_permissions.command_blacklist
                if list_all_cmds:
                    self.commands.append('{}{}'.format(self.config.command_prefix, command_name))

                elif blacklist and command_name in blacklist:
                    pass

                elif whitelist and command_name not in whitelist:
                    pass

                else:
                    self.commands.append("{}{}".format(self.config.command_prefix, command_name))

    async def on_voice_state_update(self, member, before, after):
        if not self.init_ok:
            return  # Ignore stuff before ready

        if before.channel:
            channel = before.channel
        elif after.channel:
            channel = after.channel
        else:
            return

        if not self.config.auto_pause:
            return

        autopause_msg = "{state} in {channel.guild.name}/{channel.name} {reason}"

        auto_paused = self.server_specific_data[channel.guild]['auto_paused']
        player = await self.get_player(channel)

        if not player:
            return

        if not member == self.user:  # if the user is not the bot
            if player.voice_client.channel != before.channel and player.voice_client.channel == after.channel:  # if the person joined
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state = "リジューム中",
                        channel = player.voice_client.channel,
                        reason = ""
                    ).strip())

                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = False
                    player.resume()
            elif player.voice_client.channel == before.channel and player.voice_client.channel != after.channel:
                if len(player.voice_client.channel.members) == 0:
                    if not auto_paused and player.is_playing:
                        log.info(autopause_msg.format(
                            state = "一時停止中",
                            channel = player.voice_client.channel,
                            reason = "(empty channel)"
                        ).strip())

                        self.server_specific_data[player.voice_client.guild]['auto_paused'] = True
                        player.pause()
        else:
            if len(player.voice_client.channel.members) > 0:  # channel is not empty
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state = "リジューム中",
                        channel = player.voice_client.channel,
                        reason = ""
                    ).strip())
 
                    self.server_specific_data[player.voice_client.guild]['auto_paused'] = False
                    player.resume()

    async def on_guild_update(self, before:discord.Guild, after:discord.Guild):
        if before.region != after.region:
            log.warning("サーバー \"%s\" がDiscordサーバーのホストを変更しました: %s -> %s" % (after.name, before.region, after.region))

    async def on_guild_join(self, guild:discord.Guild):
        log.info("Botがサーバーに追加されました: {}".format(guild.name))
        owner = self._get_owner(voice=True) or self._get_owner()
        if self.config.leavenonowners:
            check = guild.get_member(owner.id)
            if check == None:
                await guild.leave()
                log.info('管理者が居なかったため、{}から退出しました。'.format(guild.name))
                await owner.send(self.str.get('left-no-owner-guilds', '管理者が居なかったため、{}から退出しました。'.format(guild.name)))

        log.debug("Creating data folder for guild %s", guild.id)
        pathlib.Path('data/%s/' % guild.id).mkdir(exist_ok=True)

    async def on_guild_remove(self, guild:discord.Guild):
        log.info("Botがサーバーから削除されました: {}".format(guild.name))
        log.debug('新しいサーバー一覧:')
        [log.debug(' - ' + s.name) for s in self.guilds]

        if guild.id in self.players:
            self.players.pop(guild.id).kill()


    async def on_guild_available(self, guild:discord.Guild):
        if not self.init_ok:
            return # Ignore pre-ready events

        log.debug("サーバー \"{}\" が利用できるようになりました。".format(guild.name))

        player = self.get_player_in(guild)

        if player and player.is_paused:
            av_paused = self.server_specific_data[guild]['availability_paused']

            if av_paused:
                log.debug("Resuming player in \"{}\" due to availability.".format(guild.name))
                self.server_specific_data[guild]['availability_paused'] = False
                player.resume()


    async def on_guild_unavailable(self, guild:discord.Guild):
        log.debug("サーバー \"{}\" が利用できなくなりました。".format(guild.name))

        player = self.get_player_in(guild)

        if player and player.is_playing:
            log.debug("Pausing player in \"{}\" due to unavailability.".format(guild.name))
            self.server_specific_data[guild]['availability_paused'] = True
            player.pause()

    def voice_client_in(self, guild):
        for vc in self.voice_clients:
            if vc.guild == guild:
                return vc
        return None
