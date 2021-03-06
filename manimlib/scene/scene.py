from time import sleep
import _thread as thread
import datetime
import inspect
import os
import random
import shutil
import subprocess
import warnings

from tqdm import tqdm as ProgressDisplay
import numpy as np
from pydub import AudioSegment

from manimlib.animation.animation import Animation
from manimlib.animation.creation import Write
from manimlib.animation.transform import MoveToTarget, ApplyMethod
from manimlib.camera.camera import Camera
from manimlib.constants import *
from manimlib.container.container import Container
from manimlib.continual_animation.continual_animation import ContinualAnimation
from manimlib.mobject.mobject import Mobject
from manimlib.mobject.svg.tex_mobject import TextMobject
from manimlib.utils.iterables import list_update
from manimlib.utils.output_directory_getters import add_extension_if_not_present
from manimlib.utils.output_directory_getters import get_image_output_directory
from manimlib.utils.output_directory_getters import get_movie_output_directory
from manimlib.utils.output_directory_getters import get_partial_movie_output_directory
from manimlib.utils.output_directory_getters import get_sorted_integer_files


class Scene(Container):
    CONFIG = {
        "camera_class": Camera,
        "camera_config": {},
        "frame_duration": LOW_QUALITY_FRAME_DURATION,
        "construct_args": [],
        "skip_animations": False,
        "write_to_movie": False,
        "save_pngs": False,
        "pngs_mode": "RGBA",
        "movie_file_extension": ".mp4",
        "always_continually_update": False,
        "random_seed": 0,
        "start_at_animation_number": None,
        "end_at_animation_number": None,
        "livestreaming": False,
        "to_twitch": False,
        "twitch_key": None,
        "output_file_name": None,
        "leave_progress_bars": False,
    }

    def __init__(self, **kwargs):
        # Perhaps allow passing in a non-empty *mobjects parameter?
        Container.__init__(self, **kwargs)
        self.camera = self.camera_class(**self.camera_config)
        self.mobjects = []
        self.continual_animations = []
        self.foreground_mobjects = []
        self.num_plays = 0
        self.frame_num = 0
        self.time = 0
        self.original_skipping_status = self.skip_animations
        self.stream_lock = False
        if self.random_seed is not None:
            random.seed(self.random_seed)
            np.random.seed(self.random_seed)

        self.init_audio()
        self.setup()
        if self.livestreaming:
            return None
        try:
            self.construct(*self.construct_args)
        except EndSceneEarlyException:
            if hasattr(self, "writing_process"):
                self.writing_process.terminate()
        self.tear_down()

        if self.write_to_movie:
            self.combine_movie_files()
        self.print_end_message()

    def handle_play_like_call(func):
        def wrapper(self, *args, **kwargs):
            self.handle_animation_skipping()
            should_write = self.write_to_movie and not self.skip_animations
            if should_write:
                self.open_movie_pipe()
                func(self, *args, **kwargs)
                self.close_movie_pipe()
            else:
                func(self, *args, **kwargs)
            self.num_plays += 1
        return wrapper

    def setup(self):
        """
        This is meant to be implement by any scenes which
        are comonly subclassed, and have some common setup
        involved before the construct method is called.
        """
        pass

    def tear_down(self):
        pass

    def setup_bases(self):
        for base in self.__class__.__bases__:
            base.setup(self)

    def construct(self):
        pass  # To be implemented in subclasses

    def __str__(self):
        return self.__class__.__name__

    def get_output_file_name(self):
        if self.output_file_name is not None:
            return self.output_file_name
        return str(self)

    def print_end_message(self):
        print("Played {} animations".format(self.num_plays))

    def set_variables_as_attrs(self, *objects, **newly_named_objects):
        """
        This method is slightly hacky, making it a little easier
        for certain methods (typically subroutines of construct)
        to share local variables.
        """
        caller_locals = inspect.currentframe().f_back.f_locals
        for key, value in list(caller_locals.items()):
            for o in objects:
                if value is o:
                    setattr(self, key, value)
        for key, value in list(newly_named_objects.items()):
            setattr(self, key, value)
        return self

    def get_attrs(self, *keys):
        return [getattr(self, key) for key in keys]

    # Sound
    def init_audio(self):
        self.includes_sound = False

    def create_audio_segment(self):
        self.audio_segment = AudioSegment.silent()

    def add_audio_segment(self, new_segment, time_offset=0):
        if not self.includes_sound:
            self.includes_sound = True
            self.create_audio_segment()
        segment = self.audio_segment
        overly_time = self.get_time() + time_offset
        if overly_time < 0:
            raise Exception("Adding sound at timestamp < 0")

        curr_end = segment.duration_seconds
        new_end = overly_time + new_segment.duration_seconds
        diff = new_end - curr_end
        if diff > 0:
            segment = segment.append(
                AudioSegment.silent(int(np.ceil(diff * 1000))),
                crossfade=0,
            )
        self.audio_segment = segment.overlay(
            new_segment, position=int(1000 * overly_time)
        )

    def add_sound(self, sound_file, time_offset=0):
        new_segment = AudioSegment.from_file(sound_file)
        self.add_audio_segment(new_segment, 0)

    # Only these methods should touch the camera

    def set_camera(self, camera):
        self.camera = camera

    def get_frame(self):
        return np.array(self.camera.get_pixel_array())

    def get_image(self):
        return self.camera.get_image()

    def set_camera_pixel_array(self, pixel_array):
        self.camera.set_pixel_array(pixel_array)

    def set_camera_background(self, background):
        self.camera.set_background(background)

    def reset_camera(self):
        self.camera.reset()

    def capture_mobjects_in_camera(self, mobjects, **kwargs):
        self.camera.capture_mobjects(mobjects, **kwargs)

    def update_frame(
            self,
            mobjects=None,
            background=None,
            include_submobjects=True,
            dont_update_when_skipping=True,
            **kwargs):
        if self.skip_animations and dont_update_when_skipping:
            return
        if mobjects is None:
            mobjects = list_update(
                self.mobjects,
                self.foreground_mobjects,
            )
        if background is not None:
            self.set_camera_pixel_array(background)
        else:
            self.reset_camera()

        kwargs["include_submobjects"] = include_submobjects
        self.capture_mobjects_in_camera(mobjects, **kwargs)

    def freeze_background(self):
        self.update_frame()
        self.set_camera(Camera(self.get_frame()))
        self.clear()
    ###

    def continual_update(self, dt):
        for mobject in self.get_mobject_family_members():
            mobject.update(dt)
        for continual_animation in self.continual_animations:
            continual_animation.update(dt)

    def wind_down(self, *continual_animations, **kwargs):
        wind_down_time = kwargs.get("wind_down_time", 1)
        for continual_animation in continual_animations:
            continual_animation.begin_wind_down(wind_down_time)
        self.wait(wind_down_time)
        # TODO, this is not done with the remove method so as to
        # keep the relevant mobjects.  Better way?
        self.continual_animations = [ca for ca in self.continual_animations if ca in continual_animations]

    def should_continually_update(self):
        if self.always_continually_update:
            return True
        if len(self.continual_animations) > 0:
            return True
        any_time_based_update = any([
            len(m.get_time_based_updaters()) > 0
            for m in self.get_mobject_family_members()
        ])
        if any_time_based_update:
            return True
        return False

    ###

    def get_time(self):
        return self.time

    def increment_time(self, d_time):
        self.time += d_time

    ###

    def get_top_level_mobjects(self):
        # Return only those which are not in the family
        # of another mobject from the scene
        mobjects = self.get_mobjects()
        families = [m.get_family() for m in mobjects]

        def is_top_level(mobject):
            num_families = sum([
                (mobject in family)
                for family in families
            ])
            return num_families == 1
        return list(filter(is_top_level, mobjects))

    def get_mobject_family_members(self):
        return self.camera.extract_mobject_family_members(self.mobjects)

    def separate_mobjects_and_continual_animations(self, mobjects_or_continual_animations):
        mobjects = []
        continual_animations = []
        for item in mobjects_or_continual_animations:
            if isinstance(item, Mobject):
                mobjects.append(item)
            elif isinstance(item, ContinualAnimation):
                mobjects.append(item.mobject)
                continual_animations.append(item)
            else:
                raise Exception("""
                    Adding/Removing something which is
                    not a Mobject or a ContinualAnimation
                 """)
        return mobjects, continual_animations

    def add(self, *mobjects_or_continual_animations):
        """
        Mobjects will be displayed, from background to foreground,
        in the order with which they are entered.
        """
        mobjects, continual_animations = self.separate_mobjects_and_continual_animations(
            mobjects_or_continual_animations
        )
        mobjects += self.foreground_mobjects
        self.restructure_mobjects(to_remove=mobjects)
        self.mobjects += mobjects
        self.continual_animations += continual_animations
        return self

    def add_mobjects_among(self, values):
        """
        So a scene can just add all mobjects it's defined up to that point
        by calling add_mobjects_among(locals().values())
        """
        mobjects = [x for x in values if isinstance(x, Mobject)]
        self.add(*mobjects)
        return self

    def remove(self, *mobjects_or_continual_animations):
        mobjects, continual_animations = self.separate_mobjects_and_continual_animations(
            mobjects_or_continual_animations
        )

        to_remove = self.camera.extract_mobject_family_members(mobjects)
        for list_name in "mobjects", "foreground_mobjects":
            self.restructure_mobjects(mobjects, list_name, False)

        self.continual_animations = [
            ca for ca in self.continual_animations if ca not in
            continual_animations and ca.mobject not in to_remove]
        return self

    def restructure_mobjects(
        self, to_remove,
        mobject_list_name="mobjects",
        extract_families=True
    ):
        """
        In cases where the scene contains a group, e.g. Group(m1, m2, m3), but one
        of its submobjects is removed, e.g. scene.remove(m1), the list of mobjects
        will be editing to contain other submobjects, but not m1, e.g. it will now
        insert m2 and m3 to where the group once was.
        """
        if extract_families:
            to_remove = self.camera.extract_mobject_family_members(to_remove)
        _list = getattr(self, mobject_list_name)
        new_list = self.get_restructured_mobject_list(_list, to_remove)
        setattr(self, mobject_list_name, new_list)
        return self

    def get_restructured_mobject_list(self, mobjects, to_remove):
        new_mobjects = []

        def add_safe_mobjects_from_list(list_to_examine, set_to_remove):
            for mob in list_to_examine:
                if mob in set_to_remove:
                    continue
                intersect = set_to_remove.intersection(mob.get_family())
                if intersect:
                    add_safe_mobjects_from_list(mob.submobjects, intersect)
                else:
                    new_mobjects.append(mob)
        add_safe_mobjects_from_list(mobjects, set(to_remove))
        return new_mobjects

    def add_foreground_mobjects(self, *mobjects):
        self.foreground_mobjects = list_update(
            self.foreground_mobjects,
            mobjects
        )
        self.add(*mobjects)
        return self

    def add_foreground_mobject(self, mobject):
        return self.add_foreground_mobjects(mobject)

    def remove_foreground_mobjects(self, *to_remove):
        self.restructure_mobjects(to_remove, "foreground_mobjects")
        return self

    def remove_foreground_mobject(self, mobject):
        return self.remove_foreground_mobjects(mobject)

    def bring_to_front(self, *mobjects):
        self.add(*mobjects)
        return self

    def bring_to_back(self, *mobjects):
        self.remove(*mobjects)
        self.mobjects = list(mobjects) + self.mobjects
        return self

    def clear(self):
        self.mobjects = []
        self.foreground_mobjects = []
        self.continual_animation = []
        return self

    def get_mobjects(self):
        return list(self.mobjects)

    def get_mobject_copies(self):
        return [m.copy() for m in self.mobjects]

    def get_moving_mobjects(self, *animations):
        # Go through mobjects from start to end, and
        # as soon as there's one that needs updating of
        # some kind per frame, return the list from that
        # point forward.
        animation_mobjects = [anim.mobject for anim in animations]
        ca_mobjects = [ca.mobject for ca in self.continual_animations]
        mobjects = self.get_mobject_family_members()
        for i, mob in enumerate(mobjects):
            update_possibilities = [
                mob in animation_mobjects,
                mob in ca_mobjects,
                len(mob.get_updaters()) > 0,
                mob in self.foreground_mobjects
            ]
            for possibility in update_possibilities:
                if possibility:
                    return mobjects[i:]
        return []

    def get_time_progression(self, run_time, n_iterations=None, override_skip_animations=False):
        if self.skip_animations and not override_skip_animations:
            times = [run_time]
        else:
            step = self.frame_duration
            times = np.arange(0, run_time, step)
        time_progression = ProgressDisplay(
            times, total=n_iterations,
            leave=self.leave_progress_bars,
        )
        return time_progression

    def get_run_time(self, animations):
        return np.max([animation.run_time for animation in animations])

    def get_animation_time_progression(self, animations):
        run_time = self.get_run_time(animations)
        time_progression = self.get_time_progression(run_time)
        time_progression.set_description("".join([
            "Animation {}: ".format(self.num_plays),
            str(animations[0]),
            (", etc." if len(animations) > 1 else ""),
        ]))
        return time_progression

    def compile_play_args_to_animation_list(self, *args):
        """
        Each arg can either be an animation, or a mobject method
        followed by that methods arguments (and potentially follow
        by a dict of kwargs for that method).
        This animation list is built by going through the args list,
        and each animation is simply added, but when a mobject method
        s hit, a MoveToTarget animation is built using the args that
        follow up until either another animation is hit, another method
        is hit, or the args list runs out.
        """
        animations = []
        state = {
            "curr_method": None,
            "last_method": None,
            "method_args": [],
        }

        def compile_method(state):
            if state["curr_method"] is None:
                return
            mobject = state["curr_method"].__self__
            if state["last_method"] and state["last_method"].__self__ is mobject:
                animations.pop()
                # method should already have target then.
            else:
                mobject.generate_target()
            #
            if len(state["method_args"]) > 0 and isinstance(state["method_args"][-1], dict):
                method_kwargs = state["method_args"].pop()
            else:
                method_kwargs = {}
            state["curr_method"].__func__(
                mobject.target,
                *state["method_args"],
                **method_kwargs
            )
            animations.append(MoveToTarget(mobject))
            state["last_method"] = state["curr_method"]
            state["curr_method"] = None
            state["method_args"] = []

        for arg in args:
            if isinstance(arg, Animation):
                compile_method(state)
                animations.append(arg)
            elif inspect.ismethod(arg):
                compile_method(state)
                state["curr_method"] = arg
            elif state["curr_method"] is not None:
                state["method_args"].append(arg)
            elif isinstance(arg, Mobject):
                raise Exception("""
                    I think you may have invoked a method
                    you meant to pass in as a Scene.play argument
                """)
            else:
                raise Exception("Invalid play arguments")
        compile_method(state)
        return animations

    def handle_animation_skipping(self):
        if self.start_at_animation_number:
            if self.num_plays == self.start_at_animation_number:
                self.skip_animations = False
        if self.end_at_animation_number:
            if self.num_plays >= self.end_at_animation_number:
                self.skip_animations = True
                raise EndSceneEarlyException()

    @handle_play_like_call
    def play(self, *args, **kwargs):
        if self.livestreaming:
            self.stream_lock = False
        if len(args) == 0:
            warnings.warn("Called Scene.play with no animations")
            return

        animations = self.compile_play_args_to_animation_list(*args)
        for animation in animations:
            # This is where kwargs to play like run_time and rate_func
            # get applied to all animations
            animation.update_config(**kwargs)
            # Anything animated that's not already in the
            # scene gets added to the scene
            if animation.mobject not in self.get_mobject_family_members():
                self.add(animation.mobject)
        moving_mobjects = self.get_moving_mobjects(*animations)

        # Paint all non-moving objects onto the screen, so they don't
        # have to be rendered every frame
        self.update_frame(excluded_mobjects=moving_mobjects)
        static_image = self.get_frame()
        for t in self.get_animation_time_progression(animations):
            for animation in animations:
                animation.update(t / animation.run_time)
            self.continual_update(dt=self.frame_duration)
            self.update_frame(moving_mobjects, static_image)
            self.add_frames(self.get_frame())
        self.mobjects_from_last_animation = [
            anim.mobject for anim in animations
        ]
        self.clean_up_animations(*animations)
        if self.skip_animations:
            self.continual_update(self.get_run_time(animations))
        else:
            self.continual_update(0)

        if self.livestreaming:
            self.stream_lock = True
            thread.start_new_thread(self.idle_stream, ())
        return self

    def idle_stream(self):
        while(self.stream_lock):
            a = datetime.datetime.now()
            self.update_frame()
            n_frames = 1
            frame = self.get_frame()
            self.add_frames(*[frame] * n_frames)
            b = datetime.datetime.now()
            time_diff = (b - a).total_seconds()
            if time_diff < self.frame_duration:
                sleep(self.frame_duration - time_diff)

    def clean_up_animations(self, *animations):
        for animation in animations:
            animation.clean_up(self)
        return self

    def get_mobjects_from_last_animation(self):
        if hasattr(self, "mobjects_from_last_animation"):
            return self.mobjects_from_last_animation
        return []

    def get_wait_time_progression(self, duration, stop_condition):
        if stop_condition is not None:
            time_progression = self.get_time_progression(
                duration,
                n_iterations=-1,  # So it doesn't show % progress
                override_skip_animations=True

            )
            time_progression.set_description(
                "Waiting for {}".format(stop_condition.__name__)
            )
        else:
            time_progression = self.get_time_progression(duration)
            time_progression.set_description(
                "Waiting {}".format(self.num_plays)
            )
        return time_progression

    @handle_play_like_call
    def wait(self, duration=DEFAULT_WAIT_TIME, stop_condition=None):
        if self.should_continually_update():
            time_progression = self.get_wait_time_progression(duration, stop_condition)
            for t in time_progression:
                self.continual_update(dt=self.frame_duration)
                self.update_frame()
                self.add_frames(self.get_frame())
                if stop_condition and stop_condition():
                    time_progression.close()
                    break
        elif self.skip_animations:
            # Do nothing
            return self
        else:
            self.update_frame()
            n_frames = int(duration / self.frame_duration)
            frame = self.get_frame()
            self.add_frames(*[frame] * n_frames)
        return self

    def wait_until(self, stop_condition, max_time=60):
        self.wait(max_time, stop_condition=stop_condition)

    def force_skipping(self):
        self.original_skipping_status = self.skip_animations
        self.skip_animations = True
        return self

    def revert_to_original_skipping_status(self):
        if hasattr(self, "original_skipping_status"):
            self.skip_animations = self.original_skipping_status
        return self

    def add_frames(self, *frames):
        if self.skip_animations:
            return
        self.increment_time(len(frames) * self.frame_duration)
        if self.write_to_movie:
            for frame in frames:
                if self.save_pngs:
                    self.save_image(
                        "frame" + str(self.frame_num), self.pngs_mode, True
                    )
                    self.frame_num = self.frame_num + 1
                self.writing_process.stdin.write(frame.tostring())

    # Display methods

    def show_frame(self):
        self.update_frame(dont_update_when_skipping=False)
        self.get_image().show()

    def get_image_file_path(self, name=None, dont_update=False):
        sub_dir = "images"
        output_file_name = self.get_output_file_name()
        if dont_update:
            sub_dir = output_file_name
        path = get_image_output_directory(self.__class__, sub_dir)
        file_name = add_extension_if_not_present(
            name or output_file_name, ".png"
        )
        return os.path.join(path, file_name)

    def save_image(self, name=None, mode="RGB", dont_update=False):
        path = self.get_image_file_path(name, dont_update)
        if not dont_update:
            self.update_frame(dont_update_when_skipping=False)
        image = self.get_image()
        image = image.convert(mode)
        image.save(path)

    def get_movie_file_path(self, name=None, extension=None):
        directory = get_movie_output_directory(
            self.__class__, self.camera_config, self.frame_duration
        )
        if extension is None:
            extension = self.movie_file_extension
        if name is None:
            name = self.get_output_file_name()
        file_path = os.path.join(directory, name)
        if not file_path.endswith(extension):
            file_path += extension
        return file_path

    def get_partial_movie_directory(self):
        return get_partial_movie_output_directory(
            self.__class__, self.camera_config, self.frame_duration
        )

    def open_movie_pipe(self):
        directory = self.get_partial_movie_directory()
        file_path = os.path.join(
            directory, "{}{}".format(
                self.num_plays,
                self.movie_file_extension,
            )
        )
        temp_file_path = file_path.replace(".", "_temp.")

        self.movie_file_path = file_path
        self.temp_movie_file_path = temp_file_path

        fps = int(1 / self.frame_duration)
        height = self.camera.get_pixel_height()
        width = self.camera.get_pixel_width()

        command = [
            FFMPEG_BIN,
            '-y',  # overwrite output file if it exists
            '-f', 'rawvideo',
            '-s', '%dx%d' % (width, height),  # size of one frame
            '-pix_fmt', 'rgba',
            '-r', str(fps),  # frames per second
            '-i', '-',  # The imput comes from a pipe
            '-c:v', 'h264_nvenc',
            '-an',  # Tells FFMPEG not to expect any audio
            '-loglevel', 'error',
        ]
        if self.movie_file_extension == ".mov":
            # This is if the background of the exported video
            # should be transparent.
            command += [
                '-vcodec', 'qtrle',
                # '-vcodec', 'png',
            ]
        else:
            command += [
                '-vcodec', 'libx264',
                '-pix_fmt', 'yuv420p',
            ]
        if self.livestreaming:
            if self.to_twitch:
                command += ['-f', 'flv']
                command += ['rtmp://live.twitch.tv/app/' + self.twitch_key]
            else:
                command += ['-f', 'mpegts']
                command += [STREAMING_PROTOCOL + '://' + STREAMING_IP + ':' + STREAMING_PORT]
        else:
            command += [temp_file_path]
        self.writing_process = subprocess.Popen(command, stdin=subprocess.PIPE)

    def close_movie_pipe(self):
        self.writing_process.stdin.close()
        self.writing_process.wait()
        if self.livestreaming:
            return True
        shutil.move(
            self.temp_movie_file_path,
            self.movie_file_path,
        )

    def combine_movie_files(self):
        # Manim renders the scene as many smaller movie files
        # which are then concatenated to a larger one.  The reason
        # for this is that sometimes video-editing is made easier when
        # one works with the broken up scene, which effectively has
        # cuts at all the places you might want.  But for viewing
        # the scene as a whole, one of course wants to see it as a
        # single piece.
        partial_movie_file_directory = self.get_partial_movie_directory()
        kwargs = {
            "remove_non_integer_files": True,
            "extension": self.movie_file_extension,
        }
        if self.start_at_animation_number is not None:
            kwargs["min_index"] = self.start_at_animation_number
        if self.end_at_animation_number is not None:
            kwargs["max_index"] = self.end_at_animation_number
        else:
            kwargs["remove_indices_greater_than"] = self.num_plays - 1
        partial_movie_files = get_sorted_integer_files(
            partial_movie_file_directory,
            **kwargs
        )
        # Write a file partial_file_list.txt containing all
        # partial movie files
        file_list = os.path.join(
            partial_movie_file_directory,
            "partial_movie_file_list.txt"
        )
        with open(file_list, 'w') as fp:
            for pf_path in partial_movie_files:
                if os.name == 'nt':
                    pf_path = pf_path.replace('\\', '/')

                fp.write("file {}\n".format(pf_path))

        movie_file_path = self.get_movie_file_path()
        commands = [
            FFMPEG_BIN,
            '-y',  # overwrite output file if it exists
            '-f', 'concat',
            '-safe', '0',
            '-i', file_list,
            '-c', 'copy',
            '-loglevel', 'error',
            movie_file_path
        ]
        if not self.includes_sound:
            commands.insert(-1, '-an')

        combine_process = subprocess.Popen(commands)
        combine_process.wait()
        os.remove(file_list)
        print("File ready at {}".format(movie_file_path))

        if self.includes_sound:
            sound_file_path = movie_file_path.replace(
                self.movie_file_extension, ".wav"
            )
            # Makes sure sound file length will match video file
            self.add_audio_segment(AudioSegment.silent(0))
            self.audio_segment.export(sound_file_path)
            temp_file_path = movie_file_path.replace(".", "_temp.")
            commands = commands = [
                "ffmpeg",
                "-i", movie_file_path,
                "-i", sound_file_path,
                '-y',  # overwrite output file if it exists
                "-c:v", "copy", "-c:a", "aac",
                '-loglevel', 'error',
                "-shortest",
                "-strict", "experimental",
                temp_file_path,
            ]
            subprocess.call(commands)
            shutil.move(temp_file_path, movie_file_path)
            # subprocess.call(["rm", self.temp_movie_file_path])
            subprocess.call(["rm", sound_file_path])

        print("\nAnimation ready at {}\n".format(movie_file_path))

    # TODO, this doesn't belong in Scene, but should be
    # part of some more specialized subclass optimized
    # for livestreaming
    def tex(self, latex):
        eq = TextMobject(latex)
        anims = []
        anims.append(Write(eq))
        for mobject in self.mobjects:
            anims.append(ApplyMethod(mobject.shift, 2 * UP))
        self.play(*anims)


class EndSceneEarlyException(Exception):
    pass
