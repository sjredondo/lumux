import {
  ButtonItem,
  DropdownItem,
  Field,
  Navigation,
  PanelSection,
  PanelSectionRow,
  ProgressBarWithInfo,
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

interface LumuxInfo {
  pinned: string; // the Lumux release this plugin installs
  user: string | null; // null = not installed, "" = version unknown
  system: string | null;
  action: "install" | "update" | "";
}

interface InstallStatus {
  state: "idle" | "checking" | "downloading" | "installing" | "finishing" | "done" | "error";
  busy: boolean;
  mode: "install" | "update" | "";
  progress: number | null; // 0..1, null = indeterminate
  message: string;
  detail: string;
  error: string;
  elapsed: number;
  tag: string;
  pinned: boolean;
  version: string;
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
  lumux: LumuxInfo;
  install: InstallStatus;
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
const installLumux = callable<[update: boolean], Result<InstallStatus & { started: boolean }>>("install_lumux");

// --- helpers -------------------------------------------------------------------

const POLL_MS = 1500;

function openUrl(url: string) {
  Navigation.CloseSideMenus();
  Navigation.NavigateToExternalWeb(url);
}

function describe(s: PanelStatus | null): string {
  if (!s) return "Loading…";
  if (s.install.busy) return s.install.mode === "update" ? "Updating Lumux…" : "Installing Lumux…";
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

function elapsed(sec: number): string {
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

function InstallProgress({ install }: { install: InstallStatus | null }) {
  if (!install?.busy) return null;
  return (
    <>
      <PanelSectionRow>
        <ProgressBarWithInfo
          indeterminate={install.progress === null}
          nProgress={install.progress === null ? undefined : Math.round(install.progress * 100)}
          sOperationText={install.message}
          sTimeRemaining={elapsed(install.elapsed)}
        />
      </PanelSectionRow>
      {install.detail && (
        <PanelSectionRow>
          <div style={{ fontSize: "11px", opacity: 0.7, wordBreak: "break-word" }}>{install.detail}</div>
        </PanelSectionRow>
      )}
    </>
  );
}

// --- live sliders ----------------------------------------------------------------

/**
 * A slider bound to a daemon setting. Shows the user's value until the
 * daemon reports it back: clearing it as soon as the request returned made
 * the slider jump back to the previous value (from a status poll that was
 * still in flight), so it seemed "stuck" and took several tries to move.
 */
function useLiveSetting(
  daemonValue: number | undefined,
  send: (value: number) => Promise<Result<unknown>>,
  onError: (error: string) => void,
): [number | undefined, (value: number) => void] {
  const [local, setLocal] = useState<number | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const holdUntil = useRef(0);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  useEffect(() => {
    if (local === null || daemonValue === undefined) return;
    // Converged, or the daemon changed it since (e.g. from the desktop app)
    if (Math.abs(daemonValue - local) < 0.5 || Date.now() > holdUntil.current) {
      setLocal(null);
    }
  }, [daemonValue, local]);

  const onChange = (value: number) => {
    setLocal(value);
    holdUntil.current = Date.now() + 5000;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      send(value).then((res) => {
        if (!res.ok) onError(res.error);
        // keep showing it until a poll after this reply confirms it
        holdUntil.current = Date.now() + POLL_MS * 2;
      });
    }, 250);
  };

  return [local ?? daemonValue, onChange];
}

// --- panel -----------------------------------------------------------------------

function Content() {
  const [status, setStatus] = useState<PanelStatus | null>(null);
  const [areas, setAreas] = useState<Area[]>([]);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [nodes, setNodes] = useState<PwNode[] | null>(null);
  const lastInstallState = useRef<string>("");

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
    return () => window.clearInterval(timer);
  }, [refresh]);

  const daemon = status?.daemon ?? null;
  const paired = !!daemon?.paired;
  const pctOf = (v: number | undefined) => (v === undefined ? undefined : Math.round(v * 100));
  const [pct, onBrightness] = useLiveSetting(
    pctOf(daemon?.brightness), (v) => setBrightness(v / 100), setActionError);
  const [satPct, onSaturation] = useLiveSetting(
    pctOf(daemon?.saturation), (v) => setSaturation(v / 100), setActionError);
  const install = status?.install ?? null;
  const lumux = status?.lumux ?? null;
  const installing = !!install?.busy;

  useEffect(() => {
    if (!install) return;
    const prev = lastInstallState.current;
    lastInstallState.current = install.state;
    if (prev && prev !== "done" && install.state === "done") {
      toaster.toast({ title: "Lumux", body: `${install.message}. Next: pair your Hue bridge.` });
    } else if (prev && prev !== "error" && install.state === "error") {
      toaster.toast({ title: "Lumux", body: install.mode === "update" ? "Update failed" : "Install failed" });
    }
  }, [install?.state]);

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

  const onPair = async () => {
    toaster.toast({ title: "Lumux", body: "Press the link button on your Hue bridge…" });
    const res = await run("pair", () => pair(""));
    if (res?.ok && res.result) {
      setAreas(res.result.areas ?? []);
      toaster.toast({ title: "Lumux", body: `Paired with bridge ${res.result.bridge_ip}` });
    }
  };

  const onInstall = (update: boolean) => run("install", () => installLumux(update));
  const showUpdate = lumux?.action === "update" || (installing && install?.mode === "update");
  const showInstall =
    status?.service === "missing" || lumux?.action === "install" || (installing && install?.mode === "install");

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
  const installError = install?.state === "error" ? install.error : "";
  const problem =
    actionError ||
    installError ||
    (!installing && (status?.service === "missing" || status?.service === "crashed") ? status.service_error : "") ||
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
              description="100% = Hue's maximum"
              value={pct ?? 100}
              min={0}
              max={100}
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
              description="100% = most vivid colours, 0% = as on screen"
              value={satPct ?? 100}
              min={0}
              max={100}
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

      {showUpdate && (
        <PanelSection title="Update">
          <PanelSectionRow>
            <Field
              label={`Lumux ${lumux?.pinned ?? ""} is available`}
              description={
                `Installed: ${lumux?.user || "unknown version"}. This plugin is made for Lumux ` +
                `${lumux?.pinned ?? ""}. Syncing pauses for a moment while it updates.`
              }
            />
          </PanelSectionRow>
          <InstallProgress install={install} />
          {!installing && (
            <PanelSectionRow>
              <ButtonItem layout="below" disabled={busy !== ""} onClick={() => onInstall(true)}>
                {install?.state === "error" && install.mode === "update" ? "Try again" : "Update Lumux"}
              </ButtonItem>
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      {(showInstall || (daemon && !paired)) && (
        <PanelSection title="Setup">
          {showInstall ? (
            <>
              <PanelSectionRow>
                <Field
                  label={lumux?.system != null ? "Install Lumux for this user" : "Lumux isn't installed yet"}
                  description={
                    `Downloads Lumux ${lumux?.pinned ?? ""} from GitHub, plus the GNOME runtime from Flathub ` +
                    "the first time (a few hundred MB, several minutes). Needs an internet connection."
                  }
                />
              </PanelSectionRow>
              {lumux?.system != null && (
                <PanelSectionRow>
                  <div style={{ fontSize: "12px", opacity: 0.8 }}>
                    {`Lumux ${lumux.system} is also installed system-wide (for example from Discover). ` +
                      "It is left as it is; the plugin uses the copy it installs for you. You can remove " +
                      "the system-wide one in Desktop Mode if you don't need it."}
                  </div>
                </PanelSectionRow>
              )}
              <InstallProgress install={install} />
              {!installing && (
                <PanelSectionRow>
                  <ButtonItem layout="below" disabled={busy !== ""} onClick={() => onInstall(false)}>
                    {install?.state === "error" && install.mode === "install" ? "Try again" : "Install Lumux"}
                  </ButtonItem>
                </PanelSectionRow>
              )}
              {!installing && (
                <PanelSectionRow>
                  <ButtonItem
                    layout="below"
                    disabled={busy !== ""}
                    onClick={() => run("restart", () => restartService())}
                  >
                    {busy === "restart" ? "Checking…" : "Check again"}
                  </ButtonItem>
                </PanelSectionRow>
              )}
            </>
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
        {lumux && (
          <PanelSectionRow>
            <Field
              label="Lumux Flatpak"
              description={
                (lumux.user != null ? `${lumux.user || "installed"} (user)` : "not installed for this user") +
                (lumux.system != null
                  ? ` • ${lumux.system || "installed"} system-wide${lumux.user != null ? " (not used)" : ""}`
                  : "") +
                ` • plugin is made for ${lumux.pinned}`
              }
            />
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
}

export default definePlugin(() => ({
  name: "decky-lumux",
  titleView: <div className={staticClasses.Title}>decky-lumux</div>,
  content: <Content />,
  icon: <FaLightbulb />,
  onDismount() {},
}));
