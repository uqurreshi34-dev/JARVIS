from dataclasses import dataclass
import os
import subprocess

import psutil
import win32con
import win32gui
import win32process


@dataclass(frozen=True)
class Application:
    name: str
    app_id: str


class ApplicationManager:
    def __init__(self):
        self._applications = self._load_applications()

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

            title = win32gui.GetWindowText(hwnd).strip()

            if not title:
                return

            _, process_id = win32process.GetWindowThreadProcessId(hwnd)

            try:
                process = psutil.Process(process_id)

                if self._matches_application(process, app):
                    windows.append(hwnd)

            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess,
            ):
                pass

        win32gui.EnumWindows(callback, None)

        return windows

    def _matches_application(self, process, app):
        executable = self._get_executable(process)

        if not executable:
            return False

        executable_name = os.path.splitext(
            os.path.basename(executable)
        )[0].casefold()

        application_name = app.name.casefold()

        if executable_name == application_name:
            return True

        return self._matches_file_metadata(
            executable,
            application_name,
        )

    @staticmethod
    def _get_executable(process):
        try:
            return process.exe()
        except (
            psutil.NoSuchProcess,
            psutil.AccessDenied,
            psutil.ZombieProcess,
        ):
            return None

    @staticmethod
    def _matches_file_metadata(executable, application_name):
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    (
                        f"(Get-Item -LiteralPath "
                        f"'{executable}').VersionInfo.FileDescription"
                    ),
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            description = result.stdout.strip().casefold()

            return (
                bool(description)
                and application_name in description
            )

        except (subprocess.SubprocessError, OSError):
            return False
