"""
    pyIDM

    multi-connections internet download manager, based on "pyCuRL/curl", "youtube_dl", and "PySimpleGUI"

    :copyright: (c) 2019-2020 by Mahmoud Elshahat.
    :license: GNU LGPLv3, see LICENSE for more details.
"""
import copy
import os
import re
import zipfile
import time
from urllib.parse import urljoin

from . import config
from .downloaditem import DownloadItem, Segment
from .utils import (log, validate_file_name, get_headers, size_format, run_command, size_splitter, get_seg_size,
                    delete_file, download, process_thumbnail, execute_command, rename_file)

# youtube-dl
ytdl = None  # youtube-dl will be imported in a separate thread to save loading time


class Logger(object):
    """used for capturing youtube-dl stdout/stderr output"""

    def debug(self, msg):
        log(msg)

    def error(self, msg):
        # filter an error message when quitting youtube-dl by setting config.ytdl_abort
        if msg == "ERROR: 'NoneType' object has no attribute 'headers'": return
        log(msg)

    def warning(self, msg):
        log(msg)

    def __repr__(self):
        return "youtube-dl Logger"


def get_ytdl_options():
    ydl_opts = {'ignoreerrors': True, 'logger': Logger()}  # 'prefer_insecure': False, 'no_warnings': False,
    if config.proxy:
        ydl_opts['proxy'] = config.proxy

    # set Referer website
    if config.referer_url:
        # this is not accessible via youtube-dl options, changing standard headers is the only way
        ytdl.utils.std_headers['Referer'] = config.referer_url

    # website authentication
    if config.username or config.password:
        ydl_opts['username'] = config.username
        ydl_opts['password'] = config.password

    # cookies: https://github.com/ytdl-org/youtube-dl/blob/master/README.md#how-do-i-pass-cookies-to-youtube-dl
    if config.use_cookies:
        ydl_opts['cookiefile'] = config.cookie_file_path

    # subtitle
    # ydl_opts['listsubtitles'] = True  # this is has a problem with playlist
    # ydl_opts['allsubtitles'] = True  # has no effect
    ydl_opts['writesubtitles'] = True
    ydl_opts['writeautomaticsub'] = True


    # if config.log_level >= 3:
        # ydl_opts['verbose'] = True  # it make problem with Frozen PyIDM, extractor doesn't work
    # elif config.log_level <= 1:
    #     ydl_opts['quiet'] = True  # it doesn't work

    return ydl_opts


class Video(DownloadItem):
    """represent a youtube video object, interface for youtube-dl"""

    def __init__(self, url, vid_info=None):
        super().__init__(folder=config.download_folder)
        self.url = url
        self.resumable = True
        self.vid_info = vid_info  # a youtube-dl dictionary contains video information

        # let youtube-dl fetch video info
        if self.vid_info is None:
            with ytdl.YoutubeDL(get_ytdl_options()) as ydl:
                self.vid_info = ydl.extract_info(self.url, download=False, process=True)

        self.webpage_url = self.vid_info.get('webpage_url', None) or url

        # update url again if webpage url is available
        self.url = self.webpage_url or self.url

        self.title = validate_file_name(self.vid_info.get('title', f'video{int(time.time())}'))
        self.name = self.title

        # streams
        self.all_streams = []
        self.stream_menu = []
        self.stream_menu_map = []
        self.names_map = {'mp4_videos': [], 'other_videos': [], 'audio_streams': [], 'extra_streams': []}

        self._selected_stream = None

        # thumbnail
        self.thumbnail_url = ''

        # flag for processing raw video info by youtube-dl
        self.processed = False

        self.setup()

    def __repr__(self):
        return f'Video object( name: {self.name}, url:{self.url}'

    def setup(self):
        # url = self.vid_info.get('url', None) or self.vid_info.get('webpage_url', None) or self.vid_info.get('id', None)

        # sometimes url is just video id when fetch playlist info with process=False, try to get complete url
        # example, playlist url: https://www.youtube.com/watch?v=ethlD9moxyI&list=PL2aBZuCeDwlSXza3YLqwbUFokwqQHpPbp
        # video url = C4C8JsgGrrY
        # After processing will get webpage url = https://www.youtube.com/watch?v=C4C8JsgGrrY
        self.url = self.vid_info.get('webpage_url', None) or self.url

        # self.webpage_url = url  # self.vid_info.get('webpage_url')
        self.name = self.title = validate_file_name(self.vid_info.get('title', f'video{int(time.time())}'))

        # thumbnail
        self.thumbnail_url = self.vid_info.get('thumbnail', '')

        # subtitles
        self.subtitles = self.vid_info.get('subtitles', {})
        self.automatic_captions = self.vid_info.get('automatic_captions', {})

        # build streams
        self._process_streams()

    def _process_streams(self):
        all_streams = [Stream(x) for x in self.vid_info['formats']]
        all_streams.reverse()  # get higher quality first

        # streams has mediatype = (normal, dash, audio)
        # arrange streams as follows: video mp4, video other formats, audio, extra formats
        video_streams = [stream for stream in all_streams if stream.mediatype != 'audio']
        audio_streams = [stream for stream in all_streams if stream.mediatype == 'audio']
        extra_streams = []

        # filter repeated video streams and prefer normal over dash
        v_names = []
        for i, stream in enumerate(video_streams[:]):
            if stream.raw_name in v_names and stream.mediatype == 'dash':
                extra_streams.append(stream)
            v_names.append(stream.raw_name)

        # sort and rebuild video streams again
        video_streams = sorted([stream for stream in video_streams if stream not in extra_streams], key=lambda stream: stream.quality, reverse=True)

        # sort video streams mp4 first
        mp4_videos = [stream for stream in video_streams if stream.extension == 'mp4']
        other_videos = [stream for stream in video_streams if stream.extension != 'mp4']

        # add another audio formats, mp3, aac, wav, ogg
        if audio_streams:
            webm = [stream for stream in audio_streams if stream.extension == 'webm']
            m4a = [stream for stream in audio_streams if stream.extension in ('m4a')]

            aac = m4a[0] if m4a else audio_streams[0]
            aac = copy.copy(aac)
            aac.extension = 'aac'

            ogg = webm[0] if webm else audio_streams[0]
            ogg = copy.copy(ogg)
            ogg.extension = 'ogg'

            mp3 = copy.copy(aac)
            mp3.extension = 'mp3'
            mp3.abr = 128

            extra_audio = [aac, ogg, mp3]
            audio_streams = extra_audio + audio_streams

        # update all streams with sorted ones
        all_streams = video_streams + audio_streams + extra_streams

        # create a name map
        names_map = {'mp4_videos': [stream.name for stream in mp4_videos],
                     'other_videos': [stream.name for stream in other_videos],
                     'audio_streams': [stream.name for stream in audio_streams],
                     'extra_streams': [stream.name for stream in extra_streams]}

        # build menu
        stream_menu = ['● Video streams:                     '] + [stream.name for stream in mp4_videos] + [stream.name for stream in other_videos]  \
                      + ['', '● Audio streams:                 '] + [stream.name for stream in audio_streams]\
                      + ['', '● Extra streams:                 '] + [stream.name for stream in extra_streams]

        # stream menu map will be used to lookup streams from stream menu, can't use dictionary to allow repeated key names
        stream_menu_map = [None] + mp4_videos + other_videos + [None, None] + audio_streams + [None, None] + extra_streams

        # update properties
        self.all_streams = all_streams
        self.stream_menu = stream_menu
        self.stream_menu_map = stream_menu_map
        self.names_map = names_map  # {'mp4_videos': [], 'other_videos': [], 'audio_streams': [], 'extra_streams': []}

    def select_stream(self, index=None, name=None, raw_name=None, update=True):
        """
        search for a stream in self.stream_menu_map
        :param index: index number from stream menu
        :param name: stream name
        :param raw_name: stream raw name
        :param update: if True it will update selected stream
        :return: stream
        """
        stream = None
        try:
            if index:
                stream = self.stream_menu_map[index]

            elif name:
                stream = [stream for stream in self.all_streams if name == stream.name][0]

            elif raw_name:
                stream = [stream for stream in self.all_streams if raw_name == stream.raw_name][0]
        except:
            stream = None

        finally:
            # update selected stream
            if update and stream:
                self.selected_stream = stream

            return stream

    @property
    def selected_stream(self):
        if not self._selected_stream:
            self._selected_stream = self.all_streams[0]  # select first stream

        return self._selected_stream

    @selected_stream.setter
    def selected_stream(self, stream):
        if type(stream) is not Stream:
            raise TypeError('value must be a Stream object')

        self._selected_stream = stream
        self.selected_quality = stream.name

        self.update_param()

    def get_thumbnail(self):
        if self.thumbnail_url and not self.thumbnail:
            self.thumbnail = process_thumbnail(self.thumbnail_url)

    def update_param(self, audio_stream=None):
        """Mainly used when select a stream for current video object"""
        # reset segments first
        self.segments.clear()
        self.total_size = 0

        # do some parameters updates
        stream = self.selected_stream
        self.name = self.title + '.' + stream.extension
        self.eff_url = stream.url
        self.size = stream.size
        self.fragment_base_url = stream.fragment_base_url
        self.fragments = stream.fragments
        self.protocol = stream.protocol
        self.format_id = stream.format_id
        self.manifest_url = stream.manifest_url
        self.resolution = stream.resolution
        self.abr = stream.abr
        self.tbr = stream.tbr

        # set type ---------------------------------------------------------------------------------------
        self.type = stream.mediatype if stream.mediatype == 'audio' else 'video'

        # set subtype
        self.subtype_list.clear()

        if stream.mediatype in ('dash', 'normal'):
            self.subtype_list.append(stream.mediatype)

        if 'm3u8' in self.protocol:
            self.subtype_list.append('hls')

        if self.fragments:
            self.subtype_list.append('fragmented')

        if 'f4m' in self.protocol:
            self.subtype_list.append('f4m')

        if 'ism' in self.protocol:
            self.subtype_list.append('ism')

        # select an audio to embed if our stream is dash video
        audio_streams = sorted([stream for stream in self.all_streams if stream.mediatype == 'audio'],
                               key=lambda stream: stream.quality, reverse=True)

        if stream.mediatype == 'dash' and audio_streams:
            # auto select audio stream if no parameter given
            if not audio_stream:
                matching_stream = [audio for audio in audio_streams if audio.extension == stream.extension
                            or (audio.extension == 'm4a' and stream.extension == 'mp4')]
                # if failed to find a matching audio, choose any one
                if matching_stream:
                    audio_stream = matching_stream[0]
                else:
                    audio_stream = audio_streams[0]

            self.audio_stream = audio_stream
            self.audio_url = audio_stream.url
            self.audio_size = audio_stream.size
            self.audio_fragment_base_url = audio_stream.fragment_base_url
            self.audio_fragments = audio_stream.fragments
            self.audio_format_id = audio_stream.format_id
        else:
            self.audio_url = None
            self.audio_fragment_base_url = None
            self.audio_fragments = None
            self.audio_format_id = None

    def refresh(self):
        """will be used in case we updated vid_info dictionary from youtube-dl"""
        # reset properties and rebuild streams
        self.setup()


def process_video_info(vid, getthumbnail=True):
    try:
        vid.busy = True
        with ytdl.YoutubeDL(get_ytdl_options()) as ydl:
            vid_info = ydl.process_ie_result(vid.vid_info, download=False)
            if vid_info:
                vid.vid_info = vid_info
                vid.refresh()

                if vid and getthumbnail:
                    vid.get_thumbnail()

                log('process_video_info()> processed url:', vid.url, log_level=3)
                vid.processed = True
            else:
                log('process_video_info()> Failed,  url:', vid.url, log_level=3)
    except Exception as e:
        log('process_video_info()> error:', e)
    finally:
        vid.busy = False


class Stream:
    def __init__(self, stream_info):
        # fetch data from youtube-dl stream_info dictionary
        self.format_id = stream_info.get('format_id', '')
        self.url = stream_info.get('url', None)
        self.player_url = stream_info.get('player_url', None)
        self.extension = stream_info.get('ext', None)
        self.width = stream_info.get('width', 0)
        self.height = stream_info.get('height', 0)
        self.fps = stream_info.get('fps', None)
        self.format_note = stream_info.get('format_note', '')
        self.acodec = stream_info.get('acodec', None)
        self.abr = stream_info.get('abr', 0)
        self.tbr = stream_info.get('tbr', 0)  # for videos == BANDWIDTH/1000
        self.size = stream_info.get('filesize', None)
        # self.quality = stream_info.get('quality', None)
        self.vcodec = stream_info.get('vcodec', None)
        self.res = stream_info.get('resolution', None)
        self.downloader_options = stream_info.get('downloader_options', None)
        self.format = stream_info.get('format', None)
        self.container = stream_info.get('container', None)

        # protocol
        self.protocol = stream_info.get('protocol', '')

        # calculate some values
        self.rawbitrate = stream_info.get('abr', 0) * 1024
        self._mediatype = None
        self.resolution = f'{self.width}x{self.height}' if (self.width and self.height) else ''

        # fragmented video streams
        self.fragment_base_url = stream_info.get('fragment_base_url', None)
        self.fragments = stream_info.get('fragments', None)

        # get missing size
        if self.fragments or 'm3u8' in self.protocol:
            # ignore fragmented streams, since the size coming from headers is for first fragment not whole file
            self.size = 0
        if not isinstance(self.size, int):
            self.size = self.get_size()

        # hls stream specific
        self.manifest_url = stream_info.get('manifest_url', '')

        # print(self.name, self.size, isinstance(self.size, int))

    def get_size(self):
        headers = get_headers(self.url)
        size = int(headers.get('content-length', 0))
        print('stream.get_size()>', self.name)
        return size

    @property
    def name(self):
        return f'   › {self.extension} - {self.quality} - {size_format(self.size)} - id:{self.format_id}'  # ¤ » ›

    @property
    def raw_name(self):
        return f'      ›  {self.extension} - {self.quality}'

    @property
    def quality(self):
        try:
            if self.mediatype == 'audio':
                return int(self.abr)
            else:
                return int(self.height)
        except:
            return 0

    def __repr__(self, include_size=True):
        return self.name

    @property
    def mediatype(self):
        if not self._mediatype:
            if self.vcodec == 'none':
                self._mediatype = 'audio'
            elif self.acodec == 'none':
                self._mediatype = 'dash'
            else:
                self._mediatype = 'normal'

        return self._mediatype


def download_ffmpeg(destination=config.sett_folder):
    """it should download ffmpeg.exe for windows os"""

    # set download folder
    config.ffmpeg_download_folder = destination

    # first check windows 32 or 64
    import platform
    # ends with 86 for 32 bit and 64 for 64 bit i.e. Win7-64: AMD64 and Vista-32: x86
    if platform.machine().endswith('64'):
        # 64 bit link
        url = 'https://github.com/pyIDM/pyIDM/releases/download/extra/ffmpeg.zip'
    else:
        # 32 bit link
        url = 'https://github.com/pyIDM/pyIDM/releases/download/extra/ffmpeg_32bit.zip'

    log('downloading: ', url)

    # create a download object, will store ffmpeg in setting folder
    # print('config.sett_folder = ', config.sett_folder)
    d = DownloadItem(url=url, folder=config.ffmpeg_download_folder)
    d.update(url)
    d.name = 'ffmpeg.zip'  # must rename it for unzip to find it
    # print('d.folder = ', d.folder)

    # post download
    d.callback = 'unzip_ffmpeg'

    # send download request to main window
    execute_command("start_download", d, silent=False)


def unzip_ffmpeg():
    log('unzip_ffmpeg:', 'unzipping')

    try:
        file_name = os.path.join(config.ffmpeg_download_folder, 'ffmpeg.zip')
        with zipfile.ZipFile(file_name, 'r') as zip_ref:  # extract zip file
            zip_ref.extractall(config.ffmpeg_download_folder)

        log('ffmpeg update:', 'delete zip file')
        delete_file(file_name)
        log('ffmpeg update:', 'ffmpeg .. is ready at: ', config.ffmpeg_download_folder)
    except Exception as e:
        log('unzip_ffmpeg: error ', e)


def check_ffmpeg():
    """check for ffmpeg availability, first: current folder, second config.global_sett_folder,
    and finally: system wide"""

    log('check ffmpeg availability?')
    found = False

    # search in current app directory then default setting folder
    try:
        for folder in [config.current_directory, config.global_sett_folder]:
            for file in os.listdir(folder):
                # print(file)
                if file == 'ffmpeg.exe':
                    found = True
                    config.ffmpeg_actual_path = os.path.join(folder, file)
                    break
            if found:  # break outer loop
                break
    except:
        pass

    # Search in the system
    if not found:
        cmd = 'where ffmpeg' if config.operating_system == 'Windows' else 'which ffmpeg'
        error, output = run_command(cmd, verbose=False)
        if not error:
            found = True

            # fix issue 47 where command line return \n\r with path
            output = output.strip()
            config.ffmpeg_actual_path = os.path.realpath(output)

    if found:
        log('ffmpeg checked ok! - at: ', config.ffmpeg_actual_path)
        return True
    else:
        log(f'can not find ffmpeg!!, install it, or add executable location to PATH, or copy executable to ',
            config.global_sett_folder, 'or', config.current_directory)


def merge_video_audio(video, audio, output, d):
    """merge video file and audio file into output file, d is a reference for current DownloadItem object"""
    log('merging video and audio')

    # ffmpeg file full location
    ffmpeg = config.ffmpeg_actual_path

    # very fast audio just copied, format must match [mp4, m4a] and [webm, webm]
    cmd1 = f'"{ffmpeg}" -loglevel error -stats -y -i "{video}" -i "{audio}" -c copy "{output}"'

    # slow, mix different formats
    cmd2 = f'"{ffmpeg}" -loglevel error -stats -y -i "{video}" -i "{audio}" "{output}"'

    verbose = True if config.log_level >= 2 else False

    # run command with shell=False if failed will use shell=True option
    error, output = run_command(cmd1, verbose=verbose, hide_window=True, d=d)

    # retry on error with cmd2
    if error:
        error, output = run_command(cmd2, verbose=verbose, hide_window=True, d=d)

    return error, output
            

def import_ytdl():
    # import youtube_dl using thread because it takes sometimes 20 seconds to get imported and impact app startup time
    start = time.time()
    global ytdl, ytdl_version
    try:
        import youtube_dl as ytdl

        # update version value
        config.ytdl_VERSION = ytdl.version.__version__

        # get a random user agent and update headers
        config.HEADERS['User-Agent'] = ytdl.utils.random_user_agent()

        # calculate loading time
        load_time = time.time() - start
        log(f'youtube-dl load_time= {int(load_time)} seconds')

        # override urlopen in YoutubeDl for interrupting youtube-dl session anytime
        def foo(func):
            def newfunc(self, *args):
                # print('urlopen started ............................................')
                if config.ytdl_abort:
                    # print('urlopen aborted ............................................')
                    return None
                data = func(self, *args)
                return data

            return newfunc

        ytdl.YoutubeDL.urlopen = foo(ytdl.YoutubeDL.urlopen)

    except Exception as e:
        log('import_ytdl()> error', e)


def pre_process_hls(d):
    """
    handle m3u8 manifest file, build a local m3u8 file, and build DownloadItem segments
    :param d: DownloadItem() object
    :return True if success and False if fail
    """

    log('pre_process_hls()> start processing', d.name)

    # create temp_folder if doesn't exist
    if not os.path.isdir(d.temp_folder):
        try:
            os.makedirs(d.temp_folder)
        except Exception as e:
            log('HLS pre processing Failed:', e, showpopup=True)
            return False

    # some servers will change the contents of m3u8 file dynamically, not sure how often
    # ex: https://www.dplay.co.uk/show/help-my-house-is-haunted/video/the-skirrid-inn/EHD_259618B
    # solution is to download master manifest again, then get the updated media url
    # X-STREAM: must have BANDWIDTH, X-MEDIA: must have TYPE, GROUP-ID, NAME=="language name"
    # tbr for videos calculated by youtube-dl == BANDWIDTH/1000
    def refresh_urls(m3u8_doc, m3u8_url):
        # using youtube-dl internal function
        extract_m3u8_formats = ytdl.extractor.common.InfoExtractor._parse_m3u8_formats

        # get formats list [{'format_id': 'hls-160000mp4a.40.2-spa', 'url': 'http://ex.com/exp=15...'}, ...]
        # what we need is format_id and url
        formats = extract_m3u8_formats(None, m3u8_doc, m3u8_url, m3u8_id='hls')  # not sure about  m3u8_id='hls'
        for item in formats:
            url = item.get('url')
            # url = urljoin(d.manifest_url, url)
            format_id = item.get('format_id')

            # get format id without m3u8-id "hls-"
            stripped_format_id = format_id.replace('hls-', '') if format_id.startswith('hls-') else format_id

            # video check
            if d.format_id and (d.format_id == format_id or stripped_format_id in d.format_id):
                # print('old video url, new video url:\n', d.eff_url, '\n', url)
                d.eff_url = url

            # audio check
            if d.audio_format_id and (d.audio_format_id == format_id or stripped_format_id in d.audio_format_id):
                # print('old video url, new video url:\n', d.audio_url, '\n', url)
                d.audio_url = url

    def not_supported(m3u8_doc):
        # return msg if there is un supported protocol found in the m3u8 file

        if m3u8_doc:
            # SAMPLE-AES is not supported by ffmpeg, and mostly this will be a protected DRM stream, which shouldn't be downloaded
            if '#EXT-X-KEY:METHOD=SAMPLE-AES' in m3u8_doc:
                return 'Error: SAMPLE-AES encryption is not supported'

        return None

    def is_encrypted(m3u8_doc):
        if m3u8_doc:
            # check if file encrypted, example: #EXT-X-KEY:METHOD=AES-128,URI="xxx",IV=0x8f6109d91fffb816bcd43fefe018db49
            if '#EXT-X-KEY' in m3u8_doc:
                return True

        return False

    log('master manifest:   ', d.manifest_url)
    master_m3u8 = download_m3u8(d.manifest_url)

    if not master_m3u8:
        log("Failed to get master m3u8 file, Probably expired link", showpopup=True)
        return False

    # get fresh urls
    refresh_urls(master_m3u8, d.manifest_url)

    log('video m3u8:        ', d.eff_url)
    video_m3u8 = download_m3u8(d.eff_url)

    # abort if no video_m3u8
    if not video_m3u8:
        log("Failed to get valid m3u8 file", showpopup=True)
        return False

    audio_m3u8 = None
    if 'dash' in d.subtype_list:
        log('audio m3u8:        ', d.audio_url)
        audio_m3u8 = download_m3u8(d.audio_url)

    # save master m3u8 file for debugging, and update subtitles
    if master_m3u8:
        name = 'master.m3u8'
        local_file = os.path.join(d.temp_folder, name)
        with open(os.path.join(d.temp_folder, local_file), 'w') as f:
            f.write(master_m3u8)

    # save remote m3u8 files to disk
    with open(os.path.join(d.temp_folder, 'remote_video.m3u8'), 'w') as f:
        f.write(video_m3u8)

    if 'dash' in d.subtype_list:
        with open(os.path.join(d.temp_folder, 'remote_audio.m3u8'), 'w') as f:
            f.write(audio_m3u8)

    # check if m3u8 file has unsupported protocols
    for m3u8_doc in (video_m3u8, audio_m3u8):
        x = not_supported(m3u8_doc)
        if x:
            log(x, showpopup=True)
            return False

    # check if file is encrypted
    if is_encrypted(video_m3u8) and 'encrypted' not in d.subtype_list:
        d.subtype_list.append('encrypted')

    log(d.subtype_list)

    # ---------------------------------------------------------------------------------------------------------

    # process remote m3u8 files -------------------------------------------------------------------------------
    def process_m3u8(file, type_='video'):
        """
        process m3u8 file, extract urls, build local m3u8 file, and build segments for download item
        :param file: m3u8 as a file object
        :param type_: 'video' or 'audio'
        :return: None
        """

        base_url = d.eff_url if type_=='video' else d.audio_url
        name_prefix = 'v' if type_ == 'video' else 'a'

        url_list = []
        lines_with_local_paths = []
        lines_with_abs_urls = []
        lines = file.splitlines()
        segments = []
        merge = 'encrypted' not in d.subtype_list  # merge non-encrypted streams only

        # iterate over all m3u8 file lines
        for i, line in enumerate(lines[:]):
            url = ''
            seg_name = os.path.join(d.temp_folder, f'{name_prefix}{i + 1}')
            line_with_abs_url = line
            line_with_local_path = line

            # remove ads in m3u8 file
            if line.startswith('#ANVATO-SEGMENT-INFO') and 'type=ad' in line \
                    or line.startswith('#UPLYNK-SEGMENT') and line.endswith(',ad'):
                continue

            # lines doesn't start with # is a media links
            if line and not line.startswith('#'):
                url = line

            # handle buried urls inside lines ex: # '#EXT-X-KEY:METHOD=AES-128,URI="https://content-aus...62a9",IV=0x0000'
            elif line.startswith('#'):
                match = re.search(r'URI="(.*)"', line)
                if match:
                    url = match.group(1)

            if url:
                # get absolute url, and append it to url list for later use to build segments
                if url.startswith('skd://'):
                    # replace skd:// with https://
                    abs_url = url.replace('skd://', 'https://')
                else:
                    abs_url = urljoin(base_url, url)
                url_list.append(abs_url)

                # build line with absolute url instead of relative url
                line_with_abs_url = line.replace(url, abs_url)

                # build line with local file path instead of url
                line_with_local_path = line.replace(url, seg_name)
                line_with_local_path = line_with_local_path.replace('\\', '/')  # required for ffmpeg to work properly

                # create segment object
                segment = [Segment(name=seg_name, num=i, range=None, size=0, url=abs_url, tempfile=d.temp_file, merge=merge)]
                segments += segment

            # append to list
            lines_with_abs_urls.append(line_with_abs_url)
            lines_with_local_paths.append(line_with_local_path)

        d.segments += segments

        # write m3u8 file with absolute paths for debugging
        name = 'remote_video2.m3u8' if type_ == 'video' else 'remote_audio2.m3u8'
        file_path = os.path.join(d.temp_folder, name)
        with open(os.path.join(d.temp_folder, file_path), 'w') as f:
            f.write('\n'.join(lines_with_abs_urls))

        # write local m3u8 file
        name = 'local_video.m3u8' if type_ == 'video' else 'local_audio.m3u8'
        file_path = os.path.join(d.temp_folder, name)
        with open(os.path.join(d.temp_folder, file_path), 'w') as f:
            f.write('\n'.join(lines_with_local_paths))

    # reset segments first
    d.segments = []

    # send video m3u8 file for processing
    process_m3u8(video_m3u8, type_='video')

    # send audio m3u8 file for processing
    if 'dash' in d.subtype_list:
        process_m3u8(audio_m3u8, type_='audio')

    log('pre_process_hls()> done processing', d.name)

    return True


def post_process_hls(d):
    """ffmpeg will process m3u8 files"""

    log('post_process_hls()> start processing', d.name)

    local_video_m3u8_file = os.path.join(d.temp_folder, 'local_video.m3u8')
    local_audio_m3u8_file = os.path.join(d.temp_folder, 'local_audio.m3u8')

    cmd = f'"{config.ffmpeg_actual_path}" -loglevel error -stats -y -protocol_whitelist "file,http,https,tcp,tls,crypto"  ' \
          f'-allowed_extensions ALL -i "{local_video_m3u8_file}" -c copy -f mp4 "file:{d.temp_file}"'

    error, output = run_command(cmd, d=d)
    if error:
        log('post_process_hls()> ffmpeg failed:', output)
        return False

    if 'dash' in d.subtype_list:
        cmd = f'"{config.ffmpeg_actual_path}" -loglevel error -stats -y -protocol_whitelist "file,http,https,tcp,tls,crypto"  ' \
              f'-allowed_extensions ALL -i "{local_audio_m3u8_file}" -c copy -f mp4 "file:{d.audio_file}"'

        error, output = run_command(cmd, d=d)
        if error:
            log('post_process_hls()> ffmpeg failed:', output)
            return False

    log('post_process_hls()> done processing', d.name)

    return True


def convert_audio(d):
    """
    convert audio formats
    :param d: DownloadItem object
    :return: bool True for success or False when failed
    """
    # famous formats: mp3, aac, wav, ogg
    infile = d.temp_file
    outfile = d.target_file

    # look for compatible formats and use "copy" parameter for faster processing
    cmd1 = f'ffmpeg -loglevel error -stats -y -i "{infile}" -acodec copy "{outfile}"'

    # general command, consume time
    cmd2 = f'ffmpeg -loglevel error -stats -y -i "{infile}" "{outfile}"'

    # run command1
    error, _ = run_command(cmd1, verbose=True, hide_window=True, d=d)

    if error:
        error, _ = run_command(cmd2, verbose=True, hide_window=True, d=d)

    if error:
        return False
    else:
        return True


# parse m3u8 lines
def parse_m3u8_line(line):
    """extract attributes from m3u8 lines, source youtube-dl, utils.py"""
    # get a dictionary of attributes from line
    # examples:
    # {'TYPE': 'AUDIO', 'GROUP-ID': '160000mp4a.40.2', 'LANGUAGE': 'eng', 'NAME': 'eng'}
    # {'BANDWIDTH': '233728', 'AVERAGE-BANDWIDTH': '233728', 'RESOLUTION': '320x180', 'FRAME-RATE': '25.000', 'VIDEO-RANGE': 'SDR', 'CODECS': 'avc1.42C015,mp4a.40.2', 'AUDIO': '64000mp4a.40.2'}

    info = {}
    for (key, val) in re.findall(r'(?P<key>[A-Z0-9-]+)=(?P<val>"[^"]+"|[^",]+)(?:,|$)', line):
        if val.startswith('"'):
            val = val[1:-1]
        info[key] = val
    return info


def parse_subtitles(m3u8_doc, m3u8_url):
    # check subtitles in master m3u8, for some reasons youtube-dl doesn't recognize subtitles in m3u8 files
    # link: https://www.dplay.co.uk/show/ghost-loop/video/dead-and-breakfast/EHD_297528B
    # github issue: https://github.com/pyIDM/pyIDM/issues/77
    # if youtube-dl fixes this problem in future, there is no need for this batch
    subtitles = {}
    lines = m3u8_doc.splitlines()
    for i, line in enumerate(lines):
        info = parse_m3u8_line(line)

        # example line with subtitle: #EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="100wvtt.vtt",LANGUAGE="en",NAME="en",AUTOSELECT=YES,DEFAULT=NO,FORCED=NO,URI="exp=1587480854~ac....."
        # example parsed info: {'TYPE': 'SUBTITLES', 'GROUP-ID': '100wvtt.vtt', 'LANGUAGE': 'en', 'NAME': 'en', 'AUTOSELECT': 'YES', 'DEFAULT': 'NO', 'FORCED': 'NO', 'URI': 'exp=1587480854~ac.....'}
        if info.get('TYPE', '').lower() in ('subtitle', 'subtitles'):
            # subtitles = {language1:[sub1, sub2, ...], language2: [sub1, ...]}, where sub = {'url': 'http://x.com/s2', 'ext': 'vtt'}
            language = info.get('LANGUAGE') or info.get('NAME') or f'sub{i}'
            url = info.get('URI')
            if not url: continue

            # get absolute url
            url = urljoin(m3u8_url, url)

            # url might refer to another m3u8 file :(
            sub_m3u8 = download_m3u8(url)
            if sub_m3u8:
                # will exract first url we see
                lines = sub_m3u8.splitlines()
                sub_url = ''
                for i, line in enumerate(lines):
                    if line.startswith('#EXT-X-MEDIA'):
                        info = parse_m3u8_line(line)
                        sub_url = info.get('URI')
                    elif line.startswith('#EXTINF'):
                        sub_url = lines[i + 1]

                    if sub_url:
                        url = urljoin(m3u8_url, sub_url)
                        break
                    else:
                        continue

            # get extension
            group_id = info.get('GROUP-ID', '')  # 'GROUP-ID': '100wvtt.vtt'
            ext = os.path.splitext(group_id)[-1] if group_id else 'vtt'
            # remove "." from extension
            ext = ext.replace('.', '')

            # add sub
            subtitles.setdefault(language, [])  # set default key value if not exist
            subtitles[language].append({'url': url, 'ext': ext})
            print("{'url': url, 'ext': ext}:", {'url': url, 'ext': ext})

    return subtitles


def download_m3u8(url):
    # download the manifest from m3u8 file descriptor located at url
    buffer = download(url, verbose=False)  # get BytesIO object

    if buffer:
        # convert to string
        buffer = buffer.getvalue().decode()

        # verify file is m3u8 format
        if '#EXT' in repr(buffer):
            return buffer

    log('received invalid m3u8 file from server')
    if config.log_level >= 3:
        log('\n---------------------------------------\n', buffer, '\n---------------------------------------\n')
    return None


def download_subtitles(subs, d, ext='srt'):
    """
    download subtitles
    :param subs: expecting format template: {language1:[sub1, sub2, ...], language2: [sub1, ...]}, where sub = {'url': 'xxx', 'ext': 'xxx'}
    :param d: DownloadItem object that has the subtitles
    :param ext: subtitle format / extension
    :return: True if it completed successfully, else False
    """

    def download_sub(lang, sub):
        # sub = {'url': 'xxx', 'ext': 'xxx'}
        ext = sub.get('ext', 'txt')
        url = sub.get('url')
        file_name = f'{os.path.splitext(d.target_file)[0]}_{lang}.{ext}'

        log('downloading subtitle', file_name)
        buffer = download(url, file_name)

        if not buffer:
            log('downloading subtitle', file_name, 'failed')
            return

        # post processing 'srt' subtitle, it might be a 'vtt' file
        if ext == 'srt':
            # ffmpeg file full location
            ffmpeg = config.ffmpeg_actual_path

            output = f'{file_name}2.srt'
            log('downloading subtitle', file_name,'Ffmpeg check convert subtitle format to srt')

            cmd = f'"{ffmpeg}" -y -i "{file_name}" "{output}"'

            error, _ = run_command(cmd, verbose=False, shell=True)
            if not error:
                delete_file(file_name)
                rename_file(oldname=output, newname=file_name)
                log('created subtitle:', file_name)
            else:
                # if failed to convert
                log("couldn't convert subtitle to srt, check file format might be corrupted")

    for lang, lang_subs in subs.items():
        selected_sub = None
        for sub in lang_subs:
            # print(sub)
            if ext == sub['ext']:
                selected_sub = sub
            elif ext == 'srt' and 'vtt' == sub['ext']:
                # if vtt is available will send it as if it is srt and it will be handled in download_sub by ffmpeg
                sub['ext'] = 'srt'
                selected_sub = sub
        if lang_subs and not selected_sub:
            selected_sub = lang_subs[0]

        if selected_sub:
            download_sub(lang, selected_sub)



















