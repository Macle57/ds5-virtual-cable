import { BatteryMedium, Grid3x3 } from "lucide-react";
import { useStore } from "../../lib/store";
import type { ConfigDoc, Json } from "../../lib/types";
import { HELP, KNOWN_NOTIFICATIONS, NOTIFICATION_FIELDS } from "./help";
import { Card, Row, Toggle } from "./controls";
import GenericRows from "./GenericRows";

const asObj = (v: unknown): Record<string, Json> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, Json>) : {};

/* The `notifications` section: one switch per toast category (CONTRACT
   section 3), saved through the ordinary /api/config path. Absent keys show
   the engine's defaults, so a 0.5 config reads as it will behave once the
   1.0 tray loads it. */
export default function NotificationsSection() {
  const cfg = useStore((s) => s.cfg as ConfigDoc);
  const setTab = useStore((s) => s.setSettingsTab);
  const enabled = useStore((s) => (s.cfg?.notifications as Record<string, Json> | undefined)?.enabled !== false);
  const [master, ...rest] = NOTIFICATION_FIELDS;
  return (
    <Card title="Notifications" hint="Pick which pop-ups the tray shows. Saved as the notifications section of config.json.">
      <Row label={master[1]} keyName={master[0]} root="notifications" help={HELP[master[3]]}>
        <Toggle path={["notifications", master[0]]} dflt={master[2]} />
      </Row>
      <div className={"st-rows !mt-0 transition-opacity " + (enabled ? "" : "opacity-50")}>
        {rest.map(([key, label, dflt, helpKey]) => (
          <Row key={key} label={label} keyName={key} root="notifications" help={HELP[helpKey]}>
            <Toggle path={["notifications", key]} dflt={dflt} />
          </Row>
        ))}
      </div>
      <GenericRows path={["notifications"]} obj={asObj(cfg.notifications)} known={KNOWN_NOTIFICATIONS} />
      {/* the one toast that is a shortcut rather than an event */}
      <div className="st-row">
        <div className="st-callout !m-0" style={{ "--tone": "var(--color-circle)" } as React.CSSProperties}>
          <span className="st-callout-ico"><BatteryMedium size={17} strokeWidth={2.5} /></span>
          <div className="min-w-0 flex-1">
            <b>Battery toast on demand.</b> {HELP.show_battery} Look for it under <b>Controller</b> in any action picker.
            <div>
              <button type="button" onClick={() => setTab("chords")} className="st-link">
                <Grid3x3 size={15} strokeWidth={2.5} /> Bind it on the Chords tab
              </button>
            </div>
          </div>
        </div>
      </div>
    </Card>
  );
}
