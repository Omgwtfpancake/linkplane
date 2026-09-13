import unittest
from unittest.mock import Mock, patch

from linkplane.dependencies import (
    DependencyUnavailable,
    ScrcpyCompatibility,
    adb_dependency_plan,
    dependency_install_hint,
    dependency_plan,
    install_dependency,
    localsend_dependency_plan,
    scrcpy_compatibility,
    scrcpy_dependency_plan,
    v4l2loopback_dependency_plan,
    v4l2loopback_module_loaded,
    wayland_clipboard_dependency_plan,
)


class DependencyTests(unittest.TestCase):
    def test_dependency_plan_selects_supported_package_manager(self):
        paths = {"pacman": "/usr/bin/pacman", "sudo": "/usr/bin/sudo"}

        plan = dependency_plan("scrcpy", finder=paths.get)

        self.assertFalse(plan.available)
        self.assertEqual(plan.package_manager, "pacman")
        self.assertEqual(
            plan.install_command,
            ("sudo", "pacman", "-S", "--needed", "scrcpy"),
        )

    def test_install_dependency_runs_plan_and_verifies_executable(self):
        paths = {"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"}
        plan = dependency_plan("scrcpy", finder=paths.get)
        runner = Mock(return_value=Mock(returncode=0))

        def finder(command):
            if command == "scrcpy":
                return "/usr/bin/scrcpy"
            return paths.get(command)

        install_dependency(plan, runner=runner, finder=finder)

        runner.assert_called_once_with(
            ["sudo", "apt-get", "install", "scrcpy"], check=False
        )

    def test_adb_plan_uses_distro_specific_package_names(self):
        expected = {
            "pacman": "android-tools",
            "apt-get": "adb",
            "dnf": "android-tools",
            "brew": "android-platform-tools",
        }

        for manager, package in expected.items():
            with self.subTest(manager=manager):
                plan = adb_dependency_plan(
                    lambda command, selected=manager: (
                        f"/usr/bin/{command}"
                        if command in {selected, "sudo"}
                        else None
                    )
                )

                self.assertEqual(plan.package_manager, manager)
                self.assertEqual(plan.package, package)
                self.assertEqual(plan.install_command[-1], package)
                self.assertEqual(plan.requires_privilege, manager != "brew")
                if manager == "brew":
                    self.assertEqual(
                        plan.install_command,
                        ("brew", "install", "--cask", "android-platform-tools"),
                    )

    def test_clipboard_plan_requires_copy_and_paste_commands(self):
        paths = {
            "wl-copy": "/usr/bin/wl-copy",
            "apt-get": "/usr/bin/apt-get",
            "sudo": "/usr/bin/sudo",
        }

        plan = wayland_clipboard_dependency_plan(paths.get)

        self.assertFalse(plan.available)
        self.assertEqual(plan.required_executables, ("wl-copy", "wl-paste"))
        self.assertEqual(plan.missing_executables, ("wl-paste",))
        self.assertEqual(
            plan.install_command,
            ("sudo", "apt-get", "install", "wl-clipboard"),
        )

    def test_unsupported_automatic_install_has_explicit_reason(self):
        plan = localsend_dependency_plan(
            lambda command: "/usr/bin/apt-get" if command == "apt-get" else None
        )

        self.assertFalse(plan.available)
        self.assertIsNone(plan.install_command)
        self.assertEqual(
            dependency_install_hint(plan),
            "automatic installation is not supported with apt-get",
        )
        with self.assertRaisesRegex(DependencyUnavailable, "not supported with apt-get"):
            install_dependency(plan)

    def test_installation_verifies_all_required_commands(self):
        paths = {"apt-get": "/usr/bin/apt-get", "sudo": "/usr/bin/sudo"}
        plan = wayland_clipboard_dependency_plan(paths.get)
        runner = Mock(return_value=Mock(returncode=0))

        with self.assertRaisesRegex(DependencyUnavailable, "wl-paste"):
            install_dependency(
                plan,
                runner=runner,
                finder=lambda command: (
                    "/usr/bin/wl-copy" if command == "wl-copy" else paths.get(command)
                ),
            )

    def test_dnf_scrcpy_install_is_explicitly_unsupported(self):
        plan = scrcpy_dependency_plan(
            lambda command: "/usr/bin/dnf" if command == "dnf" else None
        )

        self.assertIsNone(plan.install_command)
        self.assertEqual(
            plan.install_error, "automatic installation is not supported with dnf"
        )

    def test_privileged_plan_requires_sudo_for_non_root_user(self):
        plan = dependency_plan(
            "adb",
            packages={"apt-get": "adb"},
            finder=lambda command: (
                "/usr/bin/apt-get" if command == "apt-get" else None
            ),
            is_root=False,
        )

        self.assertIsNone(plan.install_command)
        self.assertEqual(plan.package, "adb")
        self.assertIn("sudo is required", plan.install_error)

    def test_root_install_plan_does_not_use_sudo(self):
        plan = dependency_plan(
            "adb",
            packages={"apt-get": "adb"},
            finder=lambda command: (
                "/usr/bin/apt-get" if command == "apt-get" else None
            ),
            is_root=True,
        )

        self.assertEqual(plan.install_command, ("apt-get", "install", "adb"))
        self.assertTrue(plan.requires_privilege)

    def test_scrcpy_compatibility_checks_required_options(self):
        plan = dependency_plan(
            "scrcpy", finder=lambda command: f"/usr/bin/{command}"
        )
        help_output = (
            "--max-size --max-fps --video-bit-rate --video-buffer --audio-buffer "
            "--gamepad --no-audio --record --video-source --camera-id --camera-fps "
            "--camera-torch"
        )

        support = scrcpy_compatibility(
            plan, runner=Mock(return_value=help_output)
        )

        self.assertIsInstance(support, ScrcpyCompatibility)
        self.assertTrue(support.screen_supported)
        self.assertFalse(support.camera_supported)
        self.assertEqual(support.missing_camera_options, ("--camera-facing",))

    def test_scrcpy_compatibility_checks_webcam_options(self):
        plan = dependency_plan(
            "scrcpy", finder=lambda command: f"/usr/bin/{command}"
        )
        help_output = (
            "--max-size --max-fps --video-bit-rate --video-buffer --audio-buffer "
            "--gamepad --no-audio --record --video-source --camera-id --camera-facing "
            "--camera-fps --camera-torch --v4l2-sink --no-playback"
        )

        support = scrcpy_compatibility(plan, runner=Mock(return_value=help_output))

        self.assertTrue(support.webcam_supported)
        self.assertEqual(support.missing_webcam_options, ())

    def test_scrcpy_compatibility_reports_missing_v4l2_sink(self):
        plan = dependency_plan(
            "scrcpy", finder=lambda command: f"/usr/bin/{command}"
        )
        help_output = "--video-source --camera-facing --camera-id"

        support = scrcpy_compatibility(plan, runner=Mock(return_value=help_output))

        self.assertFalse(support.webcam_supported)
        self.assertIn("--v4l2-sink", support.missing_webcam_options)
        self.assertIn("--no-playback", support.missing_webcam_options)

    def test_v4l2loopback_plan_reports_loaded_module(self):
        plan = v4l2loopback_dependency_plan(module_loaded=lambda: True)

        self.assertTrue(plan.available)

    def test_v4l2loopback_plan_reports_missing_module_with_package_hint(self):
        plan = v4l2loopback_dependency_plan(
            lambda command: (
                f"/usr/bin/{command}" if command in {"apt-get", "sudo"} else None
            ),
            module_loaded=lambda: False,
        )

        self.assertFalse(plan.available)
        self.assertEqual(plan.package, "v4l2loopback-dkms")
        self.assertEqual(
            plan.install_command,
            ("sudo", "apt-get", "install", "v4l2loopback-dkms"),
        )

    def test_v4l2loopback_module_loaded_checks_sysfs_without_touching_disk(self):
        checked_paths = []

        def fake_exists(path):
            checked_paths.append(path)
            return False

        loaded = v4l2loopback_module_loaded(path_exists=fake_exists)

        self.assertFalse(loaded)
        self.assertEqual(checked_paths, ["/sys/module/v4l2loopback"])


if __name__ == "__main__":
    unittest.main()


class DependencyCatalogueTests(unittest.TestCase):
    """The one dependency model doctor, setup and the docs share (Slice 0, §5–§8).

    Plans are data: nothing in here may run a package manager, and the finder decides
    what "installed" means so no test depends on the machine it runs on.
    """

    @staticmethod
    def finder_with(*present):
        return lambda name: f"/usr/bin/{name}" if name in present else None

    def test_catalogue_is_well_formed(self):
        from linkplane import dependencies as deps

        names = [item.name for item in deps.dependency_catalogue()]
        self.assertEqual(len(names), len(set(names)), "duplicate dependency names")
        for item in deps.dependency_catalogue():
            with self.subTest(item=item.name):
                self.assertIn(item.purpose, deps.PURPOSES)
                self.assertIn(item.where, (deps.HOST, deps.PHONE))
                self.assertTrue(item.required_for)
                self.assertTrue(item.summary)
        # Everything the design inventory confirmed the code shells out to or needs.
        for name in ("python", "adb", "adb-usb-rules", "systemctl", "ssh", "scrcpy",
                     "v4l2loopback", "v4l2-ctl", "localsend-cli", "wl-copy", "xclip",
                     "notify-send", "termux", "termux-api", "make"):
            self.assertIn(name, names)

    def test_only_python_and_adb_block_the_usb_onboarding_path(self):
        from linkplane import dependencies as deps

        blocking = {item.name for item in deps.dependency_catalogue() if item.blocking}
        self.assertEqual(blocking, {"python", "adb", "adb-usb-rules"})
        self.assertEqual({item.purpose for item in deps.dependency_catalogue() if item.blocking},
                         {deps.CORE_REQUIRED, deps.ANDROID_BASE})

    def test_report_with_only_adb_present_is_base_ready(self):
        from linkplane import dependencies as deps

        report = deps.dependency_report(self.finder_with("adb"), python_ok=True)

        self.assertTrue(report.base_ready)
        self.assertEqual(report.required_missing, ())
        # Every optional host tool is missing and that is merely informational.
        for name in ("scrcpy", "notify-send", "v4l2-ctl", "ssh", "localsend-cli", "wl-copy", "xclip", "systemctl"):
            self.assertIn(name, report.optional_missing)
        by_name = {status.dependency.name: status for status in report.statuses}
        self.assertIsNone(by_name["termux-api"].available, "phone-side entries are not detectable here")
        self.assertIsNone(by_name["adb-usb-rules"].available, "udev rules only show at runtime")
        self.assertTrue(by_name["python"].available)
        self.assertEqual(report.to_dict()["required_missing"], [])

    def test_report_without_adb_names_exactly_what_blocks_setup(self):
        from linkplane import dependencies as deps

        report = deps.dependency_report(self.finder_with("scrcpy", "notify-send"), python_ok=True)

        self.assertFalse(report.base_ready)
        self.assertEqual(report.required_missing, ("adb",))
        self.assertNotIn("scrcpy", report.optional_missing)

    def test_report_never_runs_anything(self):
        from linkplane import dependencies as deps

        with patch("linkplane.dependencies.subprocess.run", side_effect=AssertionError("ran a process")), \
                patch("linkplane.dependencies.run_command", side_effect=AssertionError("ran a command")):
            report = deps.dependency_report(self.finder_with("adb", "pacman", "sudo"), python_ok=True)
        adb = next(status for status in report.statuses if status.dependency.name == "adb")
        self.assertTrue(adb.available)

    def test_capability_lookup_tells_the_termux_api_truth(self):
        from linkplane import dependencies as deps

        for capability in ("clipboard.read", "clipboard.write", "clipboard.sync", "camera.capture"):
            with self.subTest(capability=capability):
                names = [item.name for item in deps.dependencies_for(capability)]
                self.assertIn("termux-api", names)
                self.assertNotIn("adb", names)
        self.assertEqual([item.name for item in deps.dependencies_for("notify.post")], ["adb"])
        self.assertEqual([item.name for item in deps.dependencies_for("daemon.install")], ["systemctl"])

    def test_new_plans_carry_distro_packages(self):
        from linkplane import dependencies as deps

        cases = {
            "pacman": ("sudo pacman -S --needed libnotify", "sudo pacman -S --needed v4l-utils"),
            "apt-get": ("sudo apt-get install libnotify-bin", "sudo apt-get install v4l-utils"),
            "dnf": ("sudo dnf install libnotify", "sudo dnf install v4l-utils"),
        }
        for manager, (notify, v4l2) in cases.items():
            with self.subTest(manager=manager):
                finder = self.finder_with(manager, "sudo")
                self.assertEqual(deps.dependency_install_hint(deps.notify_send_dependency_plan(finder)), f"install it with: {notify}")
                self.assertEqual(deps.dependency_install_hint(deps.v4l2_ctl_dependency_plan(finder)), f"install it with: {v4l2}")
        systemd = deps.systemd_dependency_plan(self.finder_with("pacman", "sudo"))
        self.assertFalse(systemd.available)
        self.assertIsNone(systemd.install_command)

    def test_adb_install_plan_is_the_exact_command_setup_will_show(self):
        from linkplane import dependencies as deps

        expected = {
            "pacman": "sudo pacman -S --needed android-tools",
            "apt-get": "sudo apt-get install adb",
            "dnf": "sudo dnf install android-tools",
        }
        for manager, command in expected.items():
            with self.subTest(manager=manager):
                plan = deps.adb_dependency_plan(self.finder_with(manager, "sudo"))
                self.assertFalse(plan.available)
                self.assertEqual(" ".join(plan.install_command), command)
                self.assertTrue(plan.requires_privilege)

    def test_usb_rules_hint_names_the_package_or_the_bundling(self):
        from linkplane import dependencies as deps

        self.assertEqual(
            deps.usb_rules_install_hint(self.finder_with("pacman", "sudo")),
            "install the udev rules with: sudo pacman -S --needed android-udev; then unplug and replug the phone",
        )
        self.assertIn("ship with the adb package", deps.usb_rules_install_hint(self.finder_with("apt-get", "sudo")))
        self.assertIn("ship with the adb package", deps.usb_rules_install_hint(self.finder_with("dnf", "sudo")))
        self.assertIn("distribution's ADB udev rules", deps.usb_rules_install_hint(self.finder_with()))

    def test_scrcpy_compatibility_runner_is_resolved_at_call_time(self):
        from linkplane import dependencies as deps

        plan = deps.DependencyPlan("scrcpy", "scrcpy", True, "/usr/bin/scrcpy", None, None)
        with patch("linkplane.dependencies.run_command", return_value="--max-size --max-fps --video-bit-rate --video-buffer --audio-buffer --gamepad --no-audio --record") as run:
            support = deps.scrcpy_compatibility(plan)
        run.assert_called_once()
        self.assertTrue(support.screen_supported)
