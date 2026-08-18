from dataclasses import dataclass
import subprocess

import psutil
import pythoncom
import win32con
import win32gui
import win32process
from win32com.propsys import propsys, pscon


@dataclass(frozen=True)
class Application:
    name: str
    app_id: str


class ApplicationManager:
    def __init__(self):
        self._applications = self._load_applications()

    @property
    def applications(self):
        return tuple(application.name for application in self._applications)

    def _load_applications(self):
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-StartApps | ConvertTo-Csv -NoTypeInformation",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        applications = []

        for line in result.stdout.splitlines()[1:]:
            name, app_id = line.strip('"').split('","', maxsplit=1)

            applications.append(
                Application(
                    name=name,
                    app_id=app_id.rstrip('"'),
                )
            )

        return applications

    def find(self, name):
        query = name.casefold().strip()

        for app in self._applications:
            if app.name.casefold() == query:
                return app

        for app in self._applications:
            if query in app.name.casefold():
                return app

        return None

    def launch(self, name):
        app = self.find(name)

        if not app:
            return False

        subprocess.Popen(
            [
                "explorer.exe",
                f"shell:AppsFolder\\{app.app_id}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        return True

    def close(self, name):
        app = self.find(name)

        if not app:
            return False

        windows = self._find_application_windows(app)

        if not windows:
            return False

        for hwnd in windows:
            win32gui.PostMessage(
                hwnd,
                win32con.WM_CLOSE,
                0,
                0,
            )

        return True

    def _find_application_windows(self, app):
        windows = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return

            if self._window_belongs_to_application(hwnd, app):
                windows.append(hwnd)

        win32gui.EnumWindows(callback, None)

        return windows

    def _window_belongs_to_application(self, hwnd, app):
        window_app_id = self._get_window_app_id(hwnd)

        if window_app_id:
            return window_app_id.casefold() == app.app_id.casefold()

        return self._window_matches_process(hwnd, app)

    @staticmethod
    def _get_window_app_id(hwnd):
        try:
            pythoncom.CoInitialize()

            store = propsys.SHGetPropertyStoreForWindow(
                hwnd,
                propsys.IID_IPropertyStore,
            )

            value = store.GetValue(pscon.PKEY_AppUserModel_ID)

            return value.GetValue()

        except (OSError, AttributeError, TypeError):
            return None

        finally:
            pythoncom.CoUninitialize()

    @staticmethod
    def _window_matches_process(hwnd, app):
        _, process_id = win32process.GetWindowThreadProcessId(hwnd)

        try:
            process = psutil.Process(process_id)
            executable = process.name().casefold()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

        executable_name = executable.removesuffix(".exe")
        app_name = app.name.casefold()
        app_id = app.app_id.casefold()

        return (
            executable_name == app_name
            or executable_name in app_id
            or app_id in executable_name
        )
