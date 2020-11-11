import datetime
import fnmatch
import io
import itertools
import os
import re
import sublime
import sublime_plugin
import threading
import timeit

from typing import Any, Callable, Dict, Generator, Iterable, Iterator, List, Optional, Tuple

T_RESULT = Dict[str, Any]

PACKAGE_NAME = __package__.partition(".")[0]
TODO_SYNTAX_FILE = "Packages/{}/TodoReview.sublime-syntax".format(PACKAGE_NAME)

settings = None  # type: Optional[sublime.Settings]


def fn_to_regex(fn: str) -> str:
    """
    @brief Convert fnmatch pattern into regex pattern (directory separator safe)

    @param fn The fnmatch pattern

    @return The regex pattern
    """

    return (
        fnmatch.translate(fn)
        # match both UNIX/Windows directory separators
        .replace("/", r"[\\/]")
        # remove the \Z (i.e., $)
        .replace(r"\Z", "")
    )


def merge_regexes(regexes: Iterable[str]) -> str:
    """
    @brief Merge regexes into a single one

    @param regexes The regexes

    @return The merged regex
    """

    merged = "(?:" + ")|(?:".join(regexes) + ")"

    return "" if merged == "(?:)" else merged


class Settings:
    def __init__(self, view: sublime.View, args: Dict[str, Any]):
        self.user = sublime.load_settings("TodoReview.sublime-settings")
        if not args:
            self.proj = view.settings().get("todoreview", {})  # type: Dict[str, Any]
        else:
            self.proj = args

    def get(self, key: str, default: Any) -> Any:
        return self.proj.get(key, self.user.get(key, default))


class Engine:
    def __init__(self, dirpaths: Iterable[str], filepaths: Iterable[str], view: sublime.View):
        self.view = view
        self.dirpaths = dirpaths
        self.filepaths = set(filepaths)
        if settings.get("case_sensitive", False):
            re_case = 0
        else:
            re_case = re.IGNORECASE
        patt_patterns = settings.get("patterns", {})
        patt_files = settings.get("exclude_files", [])
        patt_folders = settings.get("exclude_folders", [])

        match_patterns = merge_regexes(patt_patterns.values())
        match_files = merge_regexes(map(fn_to_regex, patt_files))
        match_folders = merge_regexes(map(fn_to_regex, patt_folders))

        self.patterns = re.compile(match_patterns, re_case)
        self.priority = re.compile(r"\(([0-9]{1,2})\)")
        self.exclude_files = re.compile(match_files, re_case)
        self.exclude_folders = re.compile(match_folders, re_case)
        self.open = self.view.window().views()
        self.open_files = [v.file_name() for v in self.open if v.file_name()]

    def files(self) -> Generator[str, None, None]:
        seen_paths = set()
        for dirpath in self.dirpaths:
            dirpath = self.resolve(dirpath)
            for dirp, _, filepaths in os.walk(dirpath, followlinks=True):
                if self.exclude_folders.search(dirp + os.sep):
                    continue
                for filepath in filepaths:
                    self.filepaths.add(os.path.join(dirp, filepath))
        for filepath in self.filepaths:
            filepath = self.resolve(filepath)
            if filepath in seen_paths:
                continue
            if self.exclude_folders.search(filepath):
                continue
            if self.exclude_files.search(filepath):
                continue
            seen_paths.add(filepath)
            yield filepath

    def extract(self, files: Iterable[str]) -> Generator[T_RESULT, None, None]:
        encoding = settings.get("encoding", "utf-8")
        for p in files:
            try:
                if p in self.open_files:
                    lines = []
                    for view in self.open:
                        if view.file_name() == p:
                            lines = list(map(view.substr, view.lines(sublime.Region(0, len(view)))))
                            break
                else:
                    with io.open(p, "r", encoding=encoding) as f:
                        lines = f.readlines()

                for num, line in enumerate(lines, 1):
                    for result in self.patterns.finditer(line):
                        for patt, note in result.groupdict().items():
                            if not note and note != "":
                                continue
                            priority_match = self.priority.search(note)
                            if priority_match:
                                priority = int(priority_match.group(1))
                            else:
                                priority = 50
                            yield {
                                "file": p,
                                "patt": patt,
                                "note": note,
                                "line": num,
                                "priority": priority,
                            }
            except (IOError, UnicodeDecodeError):
                pass
            finally:
                thread.increment()

    def process(self) -> Generator[T_RESULT, None, None]:
        return self.extract(self.files())

    def resolve(self, directory: str) -> str:
        if settings.get("resolve_symlinks", True):
            return os.path.realpath(os.path.expanduser(os.path.abspath(directory)))
        else:
            return os.path.expanduser(os.path.abspath(directory))


class Thread(threading.Thread):
    def __init__(self, engine: Engine, callback: Callable):
        self.i = 0
        self.engine = engine
        self.callback = callback
        self.lock = threading.RLock()
        threading.Thread.__init__(self)

    def run(self) -> None:
        self.t_start = timeit.default_timer()
        self.thread()

    def thread(self) -> None:
        results = list(self.engine.process())
        self.callback(results, self.finish(), self.i)

    def finish(self) -> float:
        self.t_end = timeit.default_timer()
        return round(self.t_end - self.t_start, 2)

    def increment(self) -> None:
        with self.lock:
            self.i += 1
            sublime.status_message("TodoReview: {0} files scanned".format(self.i))


class TodoReviewCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit, **args: Dict[str, Any]) -> None:
        global settings, thread
        filepaths = []
        self.args = args
        window = self.view.window()
        paths = args.get("paths", [])  # type: List[str]
        settings = Settings(self.view, args.get("settings", {}))
        if args.get("current_file", False):
            file_name = self.view.file_name() or ""
            if file_name:
                paths = []
                filepaths = [file_name]
            else:
                sublime.message_dialog("TodoReview: File must be saved first")
                return
        else:
            if not paths and settings.get("include_paths", False):
                paths = settings.get("include_paths", False)
            if args.get("open_files", False):
                filepaths = [v.file_name() or "" for v in window.views() if v.file_name()]
            if not args.get("open_files_only", False):
                if not paths:
                    paths = window.folders()
                else:
                    for p in paths:
                        if os.path.isfile(p):
                            filepaths.append(p)
            else:
                paths = []
        engine = Engine(paths, filepaths, self.view)
        thread = Thread(engine, self.render)
        thread.start()

    def render(self, results: List[T_RESULT], time: int, count: int) -> None:
        self.view.run_command(
            "todo_review_render",
            {"results": results, "time": time, "count": count, "args": self.args},
        )


class TodoReviewRenderCommand(sublime_plugin.TextCommand):
    def run(
        self,
        edit: sublime.Edit,
        results: List[T_RESULT],
        time: int,
        count: int,
        args: Dict[str, Any],
    ) -> None:
        self.args = args
        self.edit = edit
        self.time = time
        self.count = count
        self.results = results
        self.sorted = self.sort()
        self.rview = self.get_view()
        self.draw_header()
        self.draw_results()
        self.window.focus_view(self.rview)
        self.args["settings"] = settings.proj
        self.rview.settings().set("review_args", self.args)

    def sort(self) -> Iterator[Tuple[str, Iterator[T_RESULT]]]:
        self.largest = 0

        for item in self.results:
            self.largest = max(len(self.draw_file(item)), self.largest)

        self.largest = min(self.largest, settings.get("render_maxspaces", 50)) + 6
        w = settings.get("patterns_weight", {})
        results = sorted(self.results, key=lambda m: (str(w.get(m["patt"].upper(), "No title")), m["priority"]))

        return itertools.groupby(results, key=lambda m: m["patt"])

    def get_view(self) -> sublime.View:
        self.window = sublime.active_window()
        for view in self.window.views():
            if view.settings().get("todo_results", False):
                view.erase(self.edit, sublime.Region(0, len(view)))
                return view
        view = self.window.new_file()
        view.set_name("TodoReview")
        view.assign_syntax(TODO_SYNTAX_FILE)
        view.set_scratch(True)
        view.settings().set("todo_results", True)
        view.settings().set("line_padding_bottom", 2)
        view.settings().set("line_padding_top", 2)
        view.settings().set("word_wrap", False)
        view.settings().set("command_mode", True)
        return view

    def draw_header(self) -> None:
        forms = settings.get("render_header_format", "%d - %c files in %t secs")
        datestr = settings.get("render_header_date", "%A %m/%d/%y at %I:%M%p")
        if not forms:
            forms = "%d - %c files in %t secs"
        if not datestr:
            datestr = "%A %m/%d/%y at %I:%M%p"
        if len(forms) == 0:
            return
        date = datetime.datetime.now().strftime(datestr)
        res = "// "
        res += forms.replace("%d", date).replace("%t", str(self.time)).replace("%c", str(self.count))
        res += "\n"
        self.rview.insert(self.edit, len(self.rview), res)

    def draw_results(self) -> None:
        data = [x[:] for x in [[]] * 2]
        for patt, items in self.sorted:
            items = list(items)
            res = "\n## %t (%n)\n".replace("%t", patt.upper()).replace("%n", str(len(items)))
            self.rview.insert(self.edit, len(self.rview), res)
            for idx, item in enumerate(items, 1):
                line = "{}. {}".format(idx, self.draw_file(item))
                res = "{}{}{}\n".format(
                    line,
                    " " * max((self.largest - len(line)), 1),
                    item["note"],
                )
                start = len(self.rview)
                self.rview.insert(self.edit, start, res)
                region = sublime.Region(start, len(self.rview))
                data[0].append(region)
                data[1].append(item)
        self.rview.add_regions("results", data[0], "")
        d = dict(("{0},{1}".format(k.a, k.b), v) for k, v in zip(data[0], data[1]))
        self.rview.settings().set("review_results", d)

    def draw_file(self, item: T_RESULT) -> str:
        if settings.get("render_include_folder", False):
            depth = settings.get("render_folder_depth", 1)
            if depth == "auto":
                f = item["file"]
                for folder in sublime.active_window().folders():
                    if f.startswith(folder):
                        f = os.path.relpath(f, folder)
                        break
                f = f.replace("\\", "/")
            else:
                f = os.path.dirname(item["file"]).replace("\\", "/").split("/")
                f = "/".join(f[-depth:] + [os.path.basename(item["file"])])
        else:
            f = os.path.basename(item["file"])
        return "%f:%l".replace("%f", f).replace("%l", str(item["line"]))


class TodoReviewResultsCommand(sublime_plugin.TextCommand):
    def run(self, edit: sublime.Edit, **args: Dict[str, Any]) -> None:
        self.settings = self.view.settings()

        if not self.settings.get("review_results"):
            return

        if args.get("open"):
            window = self.view.window()
            index = int(self.settings.get("selected_result", -1))
            result = self.view.get_regions("results")[index]
            coords = "{0},{1}".format(result.a, result.b)
            i = self.settings.get("review_results")[coords]  # type: T_RESULT
            p = "%f:%l".replace("%f", i["file"]).replace("%l", str(i["line"]))
            view = window.open_file(p, sublime.ENCODED_POSITION)
            window.focus_view(view)
            return

        if args.get("refresh"):
            args = self.settings.get("review_args")  # type: Dict[str, Any]
            self.view.run_command("todo_review", args)
            self.settings.erase("selected_result")
            return

        if args.get("direction"):
            d = args.get("direction")  # type: str
            results = self.view.get_regions("results")
            if not results:
                return
            start_arr = {"down": -1, "up": 0, "down_skip": -1, "up_skip": 0}
            dir_arr = {
                "down": 1,
                "up": -1,
                "down_skip": settings.get("navigation_forward_skip", 10),
                "up_skip": settings.get("navigation_backward_skip", 10) * -1,
            }  # type: Dict[str, int]
            sel = int(self.settings.get("selected_result", start_arr[d]))
            sel = sel + dir_arr[d]
            if sel == -1:
                target = results[len(results) - 1]
                sel = len(results) - 1
            if sel < 0:
                target = results[0]
                sel = 0
            if sel >= len(results):
                target = results[0]
                sel = 0
            target = results[sel]
            self.settings.set("selected_result", sel)
            region = target.cover(target)
            self.view.add_regions("selection", [region], "todo-list.selected", "circle", sublime.DRAW_NO_FILL)
            self.view.show(sublime.Region(region.a, region.a + 5))
            return
