"""The `input` config section: defaults that work untouched, merging that
keeps what the user did not mention, and coercion that never raises.

Same discipline as test_config.py: this section rides in the same file whose
parse failure must never cost the tray icon, so every malformed shape a hand
editor can produce has to come back as a usable default, not an exception.
Loaded standalone (no hardware stack needed) exactly like test_config does it.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path


def _load(name: str):
    try:
        return __import__(f"ds5app.{name}", fromlist=[name])
    except ImportError:
        path = Path(__file__).resolve().parents[1] / "ds5app" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"_ds5app_{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


CFG = _load("config")
logging.getLogger("ds5app.config").addHandler(logging.NullHandler())


class Defaults(unittest.TestCase):
    def test_a_default_config_carries_the_default_chords(self):
        ic = CFG.Config.from_dict({}).input
        self.assertTrue(ic.enabled)
        self.assertEqual(ic.chord_button, "ps")
        self.assertEqual(ic.chords, CFG.DEFAULT_CHORDS)
        self.assertEqual(ic.off_timer_minutes, 15.0)
        self.assertEqual(ic.double_press_ms, 400)
        self.assertTrue(ic.haptic_ack)
        self.assertEqual(ic.haptic_strength, 25)
        self.assertTrue(ic.stick_mouse_in_chord)

    def test_the_advertised_default_bindings(self):
        # The keymap docs/input-shortcuts.md promises. A change here is a
        # documentation change, not a refactor.
        d = CFG.DEFAULT_CHORDS
        self.assertEqual(d["triangle"], "pad_power_off")
        self.assertEqual(d["cross"], "media_play_pause")
        self.assertEqual(d["square"], "volume_mute")
        self.assertEqual(d["dpad_up"], "volume_up")
        self.assertEqual(d["dpad_down"], "volume_down")
        self.assertEqual(d["dpad_left"], "media_prev")
        self.assertEqual(d["dpad_right"], "media_next")
        self.assertEqual(d["l1"], "brightness_down")
        self.assertEqual(d["r1"], "brightness_up")
        self.assertEqual(d["options"], "projection_cycle")
        self.assertEqual(d["create"], "show_desktop")
        self.assertEqual(d["r3"], "show_battery")
        self.assertEqual(d["touch_click_2f"], "right_click")
        self.assertNotIn("touch_tap_2f", d)
        self.assertEqual(d["touch_slide_up"], "scroll_up")
        self.assertEqual(d["touch_slide_down"], "scroll_down")
        self.assertEqual(d["touch_slide_left"], "scroll_left")
        self.assertEqual(d["touch_slide_right"], "scroll_right")
        self.assertEqual(d["touch_pinch"], "pinch_zoom")
        self.assertEqual(d["touch_slide_left_pressed"], "alt_tab")
        self.assertEqual(d["touch_slide_right_pressed"], "alt_tab")
        self.assertEqual(d["touch_slide_up_pressed"], "task_view")
        self.assertEqual(d["touch_slide_down_pressed"], "minimize_all")
        self.assertNotIn("touch_pinch_pressed", d)
        # no 1.0 row survives in either default table
        for table in (CFG.DEFAULT_CHORDS, CFG.DEFAULT_REMOTE_CHORDS):
            for key in CFG.LEGACY_GESTURE_KEYS:
                self.assertNotIn(key, table)
        # every paired row's partner is a row, and the pairs are symmetric
        meta = {g["key"]: g for g in CFG.GESTURE_META}
        for key, g in meta.items():
            if "pair" in g:
                self.assertEqual(meta[g["pair"]]["pair"], key)
                self.assertIn(g["dir"], ("up", "down", "left", "right"))
        # every gesture row in either table is a served gesture key
        for table in (CFG.DEFAULT_CHORDS, CFG.DEFAULT_REMOTE_CHORDS):
            for key in table:
                if CFG.is_gesture_key(key):
                    self.assertIn(key, CFG.CHORD_GESTURES)
        self.assertEqual([g["key"] for g in CFG.gesture_meta()],
                         list(CFG.CHORD_GESTURES))
        for g in CFG.gesture_meta():
            self.assertIn(g["group"], ("taps", "unpressed", "pressed"))
            self.assertTrue(g["label"] and g["help"])

    def test_battery_lightbar_and_remote_defaults(self):
        ic = CFG.InputConfig()
        self.assertEqual(ic.battery.low_percent, 20)
        self.assertEqual(ic.battery.critical_percent, 10)
        self.assertEqual(ic.lightbar.dim_after_minutes, 0.0)   # ships OFF
        self.assertFalse(ic.remote.enabled)                    # ships OFF too

    def test_remote_mode_ships_off_from_dict_as_well(self):
        # Both construction paths must agree, or a saved default config would
        # flip the mode on at the next load.
        self.assertFalse(CFG.RemoteMode.from_dict({}).enabled)
        self.assertFalse(CFG.InputConfig.from_dict({}).remote.enabled)
        self.assertTrue(
            CFG.RemoteMode.from_dict({"enabled": True}).enabled)


class LegacyGestureRows(unittest.TestCase):
    """The gesture rows of 1.0 / 0.5 files are dropped on load: today's
    rows keep their defaults, whatever the old file said."""

    def merged(self, chords):
        return CFG.Config.from_dict({"input": {"chords": chords}}).input.chords

    def test_old_rows_are_dropped_and_the_defaults_hold(self):
        # This exact table was on a real machine (a 0.5-era file).
        d = self.merged({"touch_slide_horizontal": "alt_tab",
                         "touch_swipe_down": "minimize_all",
                         "touch_swipe_up": "task_view",
                         "touch_slide_vertical": "none",
                         "touch_swipe_up_pressed": "show_desktop",
                         "touch_slide_horizontal_pressed": "none"})
        for key in CFG.LEGACY_GESTURE_KEYS:
            self.assertNotIn(key, d)
        for key in CFG.CHORD_GESTURES:
            self.assertEqual(d.get(key), CFG.DEFAULT_CHORDS.get(key), key)

    def test_todays_rows_in_the_same_file_still_apply(self):
        d = self.merged({"touch_slide_vertical": "volume_up",
                         "touch_slide_down": "volume_down", "cross": "escape"})
        self.assertEqual(d["touch_slide_up"], "scroll_up")
        self.assertEqual(d["touch_slide_down"], "volume_down")
        self.assertEqual(d["cross"], "escape")

    def test_the_remote_table_drops_them_too(self):
        rm = CFG.Config.from_dict({"input": {"remote": {"chords": {
            "touch_slide_horizontal": "alt_tab"}}}}).input.remote
        self.assertEqual(rm.chords["touch_slide_left"], "scroll_left")
        self.assertNotIn("touch_slide_horizontal", rm.chords)

    def test_the_file_loses_them_on_the_next_save(self):
        ic = CFG.Config.from_dict({"input": {"chords": {
            "touch_swipe_up_pressed": "show_desktop"}}}).input
        out = ic.to_dict()["chords"]
        for key in CFG.LEGACY_GESTURE_KEYS:
            self.assertNotIn(key, out)
        self.assertEqual(out["touch_slide_up_pressed"], "task_view")

    def test_gesture_feel_fields(self):
        g = CFG.GestureTuning.from_dict({"pinch_delay_ms": 5000})
        self.assertEqual(g.pinch_delay_ms, 120)           # out of range: default
        self.assertEqual(CFG.GestureTuning.from_dict({"pinch_delay_ms": 0}).pinch_delay_ms, 0)
        self.assertEqual(CFG.GestureTuning().pinch_delay_ms, 120)
        self.assertEqual(CFG.GestureTuning().to_dict()["pinch_delay_ms"], 120)
        g = CFG.GestureTuning.from_dict({"scroll_sensitivity": "2.5",
                                         "scroll_reverse": "yes",
                                         "zoom_sensitivity": 99,
                                         "zoom_reverse": 0})
        self.assertEqual(g.scroll_sensitivity, 2.5)
        self.assertTrue(g.scroll_reverse)
        self.assertEqual(g.zoom_sensitivity, 10.0)      # capped
        self.assertFalse(g.zoom_reverse)
        d = CFG.GestureTuning().to_dict()
        self.assertEqual(d["scroll_sensitivity"], 1.0)
        self.assertFalse(d["scroll_reverse"])
        self.assertEqual(d["zoom_sensitivity"], 1.0)
        self.assertFalse(d["zoom_reverse"])


class Merging(unittest.TestCase):
    def test_one_named_chord_changes_one_and_keeps_the_rest(self):
        ic = CFG.InputConfig.from_dict({"chords": {"triangle": "media_next"}})
        self.assertEqual(ic.chords["triangle"], "media_next")
        self.assertEqual(ic.chords["cross"], "media_play_pause")

    def test_none_and_empty_remove_a_binding(self):
        ic = CFG.InputConfig.from_dict(
            {"chords": {"cross": "none", "square": ""}})
        self.assertNotIn("cross", ic.chords)
        self.assertNotIn("square", ic.chords)
        self.assertIn("triangle", ic.chords)

    def test_new_keys_and_names_are_kept_lowercased(self):
        ic = CFG.InputConfig.from_dict({"chords": {"R3": "Volume_Mute"}})
        self.assertEqual(ic.chords["r3"], "volume_mute")

    def test_chords_of_the_wrong_type_keep_the_defaults(self):
        ic = CFG.InputConfig.from_dict({"chords": ["triangle"]})
        self.assertEqual(ic.chords, CFG.DEFAULT_CHORDS)


class Coercion(unittest.TestCase):
    """Garbage in, defaults out -- never an exception."""

    def test_the_whole_section_survives_being_a_list(self):
        cfg = CFG.Config.from_dict({"input": [1, 2, 3]})
        self.assertEqual(cfg.input.chord_button, "ps")

    def test_bad_scalars_fall_back(self):
        ic = CFG.InputConfig.from_dict({
            "chord_button": "warp", "double_press_ms": "soon",
            "tap_replay_ms": -5, "off_timer_minutes": "forever",
            "repeat_ms": 999999,
        })
        self.assertEqual(ic.chord_button, "ps")
        self.assertEqual(ic.double_press_ms, 400)
        self.assertEqual(ic.tap_replay_ms, 100)
        self.assertEqual(ic.off_timer_minutes, 15.0)
        self.assertEqual(ic.repeat_ms, 150)

    def test_off_timer_zero_is_a_valid_choice_not_garbage(self):
        ic = CFG.InputConfig.from_dict({"off_timer_minutes": 0})
        self.assertEqual(ic.off_timer_minutes, 0.0)

    def test_haptic_strength_out_of_range_falls_back(self):
        for bad in (-1, 101, 400, "loud", None, [50]):
            ic = CFG.InputConfig.from_dict({"haptic_strength": bad})
            self.assertEqual(ic.haptic_strength, 25, f"{bad!r}")

    def test_haptic_strength_bounds_are_valid_choices(self):
        # 0 (haptics effectively silent) and 100 (motor flat out) are choices,
        # not garbage.
        self.assertEqual(
            CFG.InputConfig.from_dict({"haptic_strength": 0}).haptic_strength, 0)
        self.assertEqual(
            CFG.InputConfig.from_dict({"haptic_strength": 100}).haptic_strength,
            100)

    def test_stick_mouse_in_chord_coerces_like_every_bool(self):
        self.assertFalse(CFG.InputConfig.from_dict(
            {"stick_mouse_in_chord": "off"}).stick_mouse_in_chord)
        self.assertTrue(CFG.InputConfig.from_dict(
            {"stick_mouse_in_chord": "banana"}).stick_mouse_in_chord)

    def test_battery_coercion(self):
        b = CFG.BatteryAlerts.from_dict({
            "low_percent": "abc", "critical_percent": 150,
            "low_color": "red", "critical_color": [300, -5, 7.9],
            "low_interval_s": 0,
        })
        self.assertEqual(b.low_percent, 20)
        self.assertEqual(b.critical_percent, 10)
        self.assertEqual(b.low_color, [255, 140, 0])
        self.assertEqual(b.critical_color, [255, 0, 7])   # clamped per channel
        self.assertEqual(b.low_interval_s, 30.0)

    def test_lightbar_dim_level_is_clamped_to_one(self):
        lp = CFG.LightbarPolicy.from_dict({"dim_level": 7})
        self.assertEqual(lp.dim_level, 1.0)

    def test_remote_coercion(self):
        rm = CFG.RemoteMode.from_dict({"mouse_speed": "fast",
                                       "lightbar_color": [1, 2]})
        self.assertEqual(rm.mouse_speed, 1.6)
        self.assertEqual(rm.lightbar_color, [255, 120, 0])

    def test_actions_params_pass_through_only_as_an_object(self):
        ic = CFG.InputConfig.from_dict({"actions": {"volume_up": {"step": 3}}})
        self.assertEqual(ic.actions, {"volume_up": {"step": 3}})
        ic = CFG.InputConfig.from_dict({"actions": "loud"})
        self.assertEqual(ic.actions, {})


class RoundTrip(unittest.TestCase):
    def test_to_dict_from_dict_is_stable(self):
        src = CFG.InputConfig.from_dict({
            "chords": {"cross": "none", "r3": "volume_mute"},
            "off_timer_minutes": 5,
            "battery": {"low_percent": 25, "custom_note": "keep me"},
            "future_key": {"x": 1},
        })
        d = json.loads(json.dumps(src.to_dict()))
        again = CFG.InputConfig.from_dict(d)
        self.assertEqual(again.to_dict(), src.to_dict())

    def test_unknown_keys_ride_along_at_every_level(self):
        src = CFG.InputConfig.from_dict({
            "future_key": 1,
            "battery": {"future_battery": 2},
            "remote": {"future_remote": 3},
            "lightbar": {"future_lightbar": 4},
        })
        d = src.to_dict()
        self.assertEqual(d["future_key"], 1)
        self.assertEqual(d["battery"]["future_battery"], 2)
        self.assertEqual(d["remote"]["future_remote"], 3)
        self.assertEqual(d["lightbar"]["future_lightbar"], 4)

    def test_a_removed_chord_stays_removed_across_save_and_load(self):
        # A removal is only durable if to_dict writes the merged table rather
        # than the user's delta -- assert the round-tripped file agrees.
        src = CFG.InputConfig.from_dict({"chords": {"cross": "none"}})
        again = CFG.InputConfig.from_dict(src.to_dict())
        self.assertNotIn("cross", again.chords)

    def test_the_whole_config_file_round_trips_the_input_section(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config.json")
        cfg = CFG.Config()
        cfg.input.chords["r3"] = "volume_mute"
        cfg.input.off_timer_minutes = 30.0
        CFG.save(cfg, path)
        back = CFG.load(path)
        self.assertEqual(back.input.chords["r3"], "volume_mute")
        self.assertEqual(back.input.off_timer_minutes, 30.0)
        # and it is real JSON a dashboard can render generically
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertIn("input", raw)
        self.assertIn("chords", raw["input"])

    def test_a_config_without_the_section_gets_the_defaults(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"enabled": True, "controllers": {}}, fh)
        back = CFG.load(path)
        self.assertEqual(back.input.chords, CFG.DEFAULT_CHORDS)


class RemoteTable(unittest.TestCase):
    """`input.remote.chords` merges over DEFAULT_REMOTE_CHORDS exactly the way
    `input.chords` merges over DEFAULT_CHORDS, and `same_bindings` rides
    next to it."""

    def test_defaults(self):
        rm = CFG.RemoteMode.from_dict({})
        self.assertTrue(rm.same_bindings)
        self.assertEqual(rm.chords, CFG.DEFAULT_REMOTE_CHORDS)
        self.assertEqual(CFG.DEFAULT_REMOTE_CHORDS["cross"], "left_click")

    def test_merge_and_removal(self):
        rm = CFG.RemoteMode.from_dict({"same_bindings": False,
                                       "chords": {"cross": "none",
                                                  "Square": "Middle_Click"}})
        self.assertFalse(rm.same_bindings)
        self.assertNotIn("cross", rm.chords)
        self.assertEqual(rm.chords["square"], "middle_click")
        self.assertEqual(rm.chords["circle"], "escape")        # untouched

    def test_removal_is_written_as_none_and_survives_a_round_trip(self):
        rm = CFG.RemoteMode.from_dict({"chords": {"cross": "none"}})
        d = rm.to_dict()
        self.assertEqual(d["chords"]["cross"], "none")
        again = CFG.RemoteMode.from_dict(d)
        self.assertNotIn("cross", again.chords)
        self.assertEqual(again.to_dict(), d)

    def test_garbage_table_keeps_the_defaults(self):
        rm = CFG.RemoteMode.from_dict({"chords": ["cross"], "same_bindings": "no"})
        self.assertEqual(rm.chords, CFG.DEFAULT_REMOTE_CHORDS)
        self.assertFalse(rm.same_bindings)

    def test_unknown_keys_survive_next_to_the_new_fields(self):
        rm = CFG.RemoteMode.from_dict({"future": 1, "chords": {}})
        d = rm.to_dict()
        self.assertEqual(d["future"], 1)
        self.assertIn("same_bindings", d)
        self.assertIn("chords", d)

    def test_the_whole_config_round_trips_the_remote_table(self):
        cfg = CFG.Config.from_dict({"input": {"remote": {
            "same_bindings": False, "chords": {"options": "volume_mute"}}}})
        d = cfg.to_dict()
        self.assertEqual(d["input"]["remote"]["chords"]["options"], "volume_mute")
        self.assertFalse(d["input"]["remote"]["same_bindings"])
        self.assertEqual(CFG.Config.from_dict(d).input.remote.chords,
                         cfg.input.remote.chords)

    def test_shared_merge_helpers(self):
        self.assertEqual(CFG.merge_chords({"a": "x"}, {"b": "y", "a": "off"}, "t"),
                         {"b": "y"})
        self.assertEqual(CFG.chords_to_dict({"a": "x"}, {"b": "y"}),
                         {"a": "none", "b": "y"})

    def test_the_new_default_chords_and_engine_action(self):
        self.assertEqual(CFG.DEFAULT_CHORDS["touchpad_click"], "keyboard")
        self.assertEqual(CFG.DEFAULT_CHORDS["mute"], "dictation")
        self.assertIn("keyboard", CFG.ENGINE_ACTIONS)


if __name__ == "__main__":
    unittest.main()
