import {
  ButtonItem,
  DropdownItem,
  Field,
  Navigation,
  PanelSection,
  PanelSectionRow,
  SliderField,
  Spinner,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import { FaLightbulb } from "react-icons/fa";

// --- backend API (main.py) ---------------------------------------------------

interface Result<T> {
  ok: boolean;
  result: T | null;
  error: string;
}

interface DaemonStatus {
  version: string;
  state: "idle" | "starting" | "syncing" | "error" | "unpaired";
  syncing: boolean;
  mode: string;
  paired: boolean;
  pairing: boolean;
  bridge: { ip: string; configured: boolean; connected: boolean };
  area_id: string;
  streaming: boolean;
  brightness: number;
  saturation?: number;
  capture: { backend: string; node: string; capturing: boolean; error: string };
  fps: number;
  last_error: string;
  last_warning: string;
}

interface PanelStatus {
  service: "stopped" | "starting" | "running" | "crashed" | "missing";
  service_error: string;
  runner: string;
  runners: { flatpak: boolean; host: boolean; custom: boolean };
  settings: { runner: string; custom_command: string; node: string; auto_start: boolean };
  daemon: DaemonStatus | null;
  help_url: string;
  version: string;
}

interface Area {
  id: string;
  name: string;
  channels: number;
}

interface PwNode {
  name: string;
  serial: string;
  id: string;
  description: string;
}

const getStatus = callable<[], PanelStatus>("get_status");
const setSync = callable<[enabled: boolean], Result<{ state: string }>>("set_sync");
const listAreas = callable<[], Result<{ areas: Area[]; selected: string }>>("list_areas");
const selectArea = callable<[areaId: string], Result<unknown>>("select_area");
const setBrightness = callable<[value: number], Result<unknown>>("set_brightness");
const setSaturation = callable<[value: number], Result<unknown>>("set_saturation");
const pair = callable<[bridgeIp: string], Result<{ bridge_ip: string; areas: Area[] }>>("pair");
const listNodes = callable<[], Result<{ nodes: PwNode[]; gamescope: PwNode | null; selected: string }>>(
  "list_nodes",
);
const setNode = callable<[node: string], Result<unknown>>("set_node");
const setSetting = callable<[key: string, value: unknown], Result<unknown>>("set_setting");
const restartService = callable<[], Result<boolean>>("restart_service");

// --- helpers -------------------------------------------------------------------

const POLL_MS = 1500;

function openUrl(url: string) {
  Navigation.CloseSideMenus();
  Navigation.NavigateToExternalWeb(url);
}

function describe(s: PanelStatus | null): string {
  if (!s) return "Loading…";
  if (s.service === "missing") return "Lumux not installed";
  if (s.service === "crashed") return "Service stopped";
  const d = s.daemon;
  if (!d) return "Starting service…";
  switch (d.state) {
    case "syncing":
      return d.streaming ? `Syncing • ${Math.round(d.fps)} fps` : "Syncing • reconnecting…";
    case "starting":
      return "Connecting to bridge…";
    case "unpaired":
      return "Not paired";
    case "error":
      return "Error";
    default:
      return d.bridge.configured ? "Ready" : "Not paired";
  }
}

function ErrorText({ text }: { text: string }) {
  if (!text) return null;
  return (
    <PanelSectionRow>
      <div style={{ color: "#ff8a80", fontSize: "12px", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
        {text}
      </div>
    </PanelSectionRow>
  );
}

// --- panel -----------------------------------------------------------------------

function Content() {
  const [status, setStatus] = useState<PanelStatus | null>(null);
  const [areas, setAreas] = useState<Area[]>([]);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [brightness, setLocalBrightness] = useState<number | null>(null);
  const [saturation, setLocalSaturation] = useState<number | null>(null);
  const saturationTimer = useRef<number | undefined>(undefined);
  const [nodes, setNodes] = useState<PwNode[] | null>(null);
  const brightnessTimer = useRef<number | undefined>(undefined);

  const refresh = useCallback(async () => {
    try {
      setStatus(await getStatus());
    } catch (e) {
      setActionError(String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    return () => {
      window.clearInterval(timer);
      window.clearTimeout(brightnessTimer.current);
      window.clearTimeout(saturationTimer.current);
    };
  }, [refresh]);

  const daemon = status?.daemon ?? null;
  const paired = !!daemon?.paired;

  const loadAreas = useCallback(async () => {
    const res = await listAreas();
    if (res.ok && res.result) setAreas(res.result.areas);
    else setActionError(res.error);
  }, []);

  useEffect(() => {
    if (paired && areas.length === 0) loadAreas();
  }, [paired, areas.length, loadAreas]);

  const run = async <T,>(name: string, fn: () => Promise<Result<T>>): Promise<Result<T> | null> => {
    setBusy(name);
    setActionError("");
    try {
      const res = await fn();
      if (!res.ok) setActionError(res.error);
      return res;
    } catch (e) {
      setActionError(String(e));
      return null;
    } finally {
      setBusy("");
      refresh();
    }
  };

  const onToggle = (enabled: boolean) => run("sync", () => setSync(enabled));

  const onArea = (areaId: string) => run("area", () => selectArea(areaId));

  const onBrightness = (pct: number) => {
    setLocalBrightness(pct);
    window.clearTimeout(brightnessTimer.current);
    brightnessTimer.current = window.setTimeout(() => {
      setBrightness(pct / 100).then((res) => {
        if (!res.ok) setActionError(res.error);
        setLocalBrightness(null);
      });
    }, 300);
  };

  const onSaturation = (pct: number) => {
    setLocalSaturation(pct);
    window.clearTimeout(saturationTimer.current);
    saturationTimer.current = window.setTimeout(() => {
      setSaturation(pct / 100).then((res) => {
        if (!res.ok) setActionError(res.error);
        setLocalSaturation(null);
      });
    }, 300);
  };

  const onPair = async () => {
    toaster.toast({ title: "Lumux", body: "Press the link button on your Hue bridge…" });
    const res = await run("pair", () => pair(""));
    if (res?.ok && res.result) {
      setAreas(res.result.areas ?? []);
      toaster.toast({ title: "Lumux", body: `Paired with bridge ${res.result.bridge_ip}` });
    }
  };

  const onDetect = async () => {
    const res = await run("nodes", () => listNodes());
    if (res?.ok && res.result) {
      setNodes(res.result.nodes);
      if (res.result.nodes.length === 0) {
        setActionError("No PipeWire video sources visible. Is Game Mode running?");
      }
    }
  };

  const syncing = !!daemon && (daemon.syncing || daemon.state === "starting");
  const canSync = !!daemon && paired && !!daemon.area_id && busy === "";
  const pct = brightness ?? Math.round((daemon?.brightness ?? 1) * 100);
  const satPct = saturation ?? Math.round((daemon?.saturation ?? 1) * 100);
  const problem =
    actionError ||
    (status?.service === "missing" || status?.service === "crashed" ? status.service_error : "") ||
    daemon?.last_error ||
    (daemon?.syncing ? daemon.capture.error || daemon.last_warning : "");

  return (
    <>
      <PanelSection title="Screen sync">
        <PanelSectionRow>
          <Field label="Status" description={daemon?.bridge.ip ? `Bridge ${daemon.bridge.ip}` : undefined}>
            {busy ? <Spinner style={{ width: "20px", height: "20px" }} /> : describe(status)}
          </Field>
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Sync lights"
            description={daemon && !daemon.area_id && paired ? "Choose an entertainment area first" : undefined}
            checked={syncing}
            disabled={!canSync && !syncing}
            onChange={onToggle}
          />
        </PanelSectionRow>
        {paired && (
          <PanelSectionRow>
            <DropdownItem
              label="Entertainment area"
              strDefaultLabel={areas.length ? "Choose…" : "Loading…"}
              rgOptions={areas.map((a) => ({ data: a.id, label: `${a.name} (${a.channels})` }))}
              selectedOption={daemon?.area_id || null}
              disabled={busy !== "" || areas.length === 0}
              onChange={(opt) => onArea(opt.data)}
            />
          </PanelSectionRow>
        )}
        {daemon && (
          <PanelSectionRow>
            <SliderField
              label="Brightness"
              value={pct}
              min={0}
              max={200}
              step={5}
              showValue
              valueSuffix="%"
              resetValue={100}
              onChange={onBrightness}
            />
          </PanelSectionRow>
        )}
        {daemon && (
          <PanelSectionRow>
            <SliderField
              label="Saturation"
              description="Higher = more vivid colours"
              value={satPct}
              min={0}
              max={300}
              step={5}
              showValue
              valueSuffix="%"
              resetValue={100}
              onChange={onSaturation}
            />
          </PanelSectionRow>
        )}
        <ErrorText text={problem} />
      </PanelSection>

      {(status?.service === "missing" || (daemon && !paired)) && (
        <PanelSection title="Setup">
          {status?.service === "missing" ? (
            <PanelSectionRow>
              <Field
                label="Install Lumux"
                description="Install the Lumux Flatpak and pair your bridge in Desktop Mode."
              />
            </PanelSectionRow>
          ) : (
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                description="Press the link button on the Hue bridge, then select this within 30 s."
                disabled={busy !== "" || !!daemon?.pairing}
                onClick={onPair}
              >
                {busy === "pair" ? "Waiting for link button…" : "Pair with Hue bridge"}
              </ButtonItem>
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => status && openUrl(status.help_url)}>
              Setup instructions
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Advanced">
        <PanelSectionRow>
          <ToggleField
            label="Sync when Game Mode starts"
            checked={!!status?.settings.auto_start}
            disabled={!status}
            onChange={(v) => run("settings", () => setSetting("auto_start", v))}
          />
        </PanelSectionRow>
        {daemon && (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              description={`Capture: ${daemon.capture.backend}${daemon.capture.node ? ` (${daemon.capture.node})` : ""}`}
              disabled={busy !== ""}
              onClick={onDetect}
            >
              Detect capture source
            </ButtonItem>
          </PanelSectionRow>
        )}
        {nodes && nodes.length > 0 && (
          <PanelSectionRow>
            <DropdownItem
              label="PipeWire video source"
              rgOptions={nodes.map((n) => ({
                data: n.serial || n.name || n.id,
                label: n.description || n.name,
              }))}
              selectedOption={daemon?.capture.node ?? null}
              onChange={(opt) => run("nodes", () => setNode(String(opt.data)))}
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            description={status ? `Service: ${status.service}${status.runner ? ` via ${status.runner}` : ""}` : undefined}
            disabled={busy !== ""}
            onClick={() => run("restart", () => restartService())}
          >
            Restart Lumux service
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "Lumux",
  titleView: <div className={staticClasses.Title}>Lumux</div>,
  content: <Content />,
  icon: <FaLightbulb />,
  onDismount() {},
}));
