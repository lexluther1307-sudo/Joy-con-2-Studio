import React, { useEffect, useState } from "react";
import { ButtonItem, PanelSection, PanelSectionRow, staticClasses } from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { FaGamepad } from "react-icons/fa";

type SideStatus = { connected: boolean; battery?: number; charging?: boolean; name?: string };
type Status = {
  pad_ok: boolean;
  pad_message: string;
  left: SideStatus;
  right: SideStatus;
  scanning: boolean;
  message: string;
  songs: string[];
  diag?: string[];
  bleak_error?: string;
  seen?: string[];
  recent?: string[];
};

const getStatus = callable<[], Status>("status");
const startScan = callable<[], boolean>("scan");
const disconnectAll = callable<[], boolean>("disconnect_all");
const calibrate = callable<[], boolean>("calibrate");
const testRumble = callable<[string], boolean>("test_rumble");
const playSong = callable<[number], boolean>("play_song");
const stopVibration = callable<[], boolean>("stop_vibration");

function sideLine(label: string, s?: SideStatus): string {
  if (!s || !s.connected) return `${label}: waiting`;
  const power = s.charging ? "charging" : "battery";
  return `${label}: connected, ${power} ${s.battery ?? 0}%`;
}

const Content = () => {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await getStatus();
        if (alive) {
          setStatus(s);
          setError("");
        }
      } catch (e) {
        if (alive) setError(String(e));
      }
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (!status) {
    return (
      <PanelSection title="Status">
        <PanelSectionRow>
          <div>{error ? `Backend error: ${error}` : "Starting the plugin..."}</div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <div>
            {status.pad_ok ? "Virtual DualShock 4: ready" : `Virtual controller: ${status.pad_message}`}
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>{sideLine("Left", status.left)}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>{sideLine("Right", status.right)}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>{status.message}</div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Joy-Cons">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => startScan()}>
            Scan for Joy-Cons
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => calibrate()}>
            Zero sticks and gyro
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => disconnectAll()}>
            Disconnect all
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Rumble">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => testRumble("R")}>
            Test rumble (right)
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => testRumble("L")}>
            Test rumble (left)
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => stopVibration()}>
            Stop vibration
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Songs">
        {(status.songs ?? []).map((name, index) => (
          <PanelSectionRow key={name}>
            <ButtonItem layout="below" onClick={() => playSong(index)}>
              {name}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </PanelSection>

      <PanelSection title="Diagnostics">
        {(status.diag ?? []).map((line, i) => (
          <PanelSectionRow key={`diag${i}`}>
            <div>{line}</div>
          </PanelSectionRow>
        ))}
        {status.bleak_error ? (
          <PanelSectionRow>
            <div>{status.bleak_error}</div>
          </PanelSectionRow>
        ) : null}
        {(status.seen ?? []).map((line, i) => (
          <PanelSectionRow key={`seen${i}`}>
            <div>Seen: {line}</div>
          </PanelSectionRow>
        ))}
        {(status.recent ?? []).map((line, i) => (
          <PanelSectionRow key={`recent${i}`}>
            <div>{line}</div>
          </PanelSectionRow>
        ))}
      </PanelSection>
    </>
  );
};

export default definePlugin(() => {
  return {
    name: "Joy-Con 2 Studio",
    titleView: <div className={staticClasses.Title}>Joy-Con 2 Studio</div>,
    content: <Content />,
    icon: <FaGamepad />,
    onDismount() {},
  };
});
