import React, { useEffect, useState } from "react";
import { ButtonItem, PanelSection, PanelSectionRow, staticClasses } from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { FaWifi } from "react-icons/fa";

type Status = {
  version: string;
  pad_ok: boolean;
  pad_message: string;
  pc_connected: boolean;
  pc_address: string;
  packets_per_second: number;
  deck_address: string;
  port: number;
  scanning: boolean;
  scan_result: string[];
  message: string;
};

const getStatus = callable<[], Status>("status");
const scanNetwork = callable<[], boolean>("scan");

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
          <div>{error ? `Backend error: ${error}` : "Starting..."}</div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <div>{`Version ${status.version}`}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>{status.pad_ok ? "Virtual DualShock 4: ready" : `Virtual controller: ${status.pad_message}`}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>
            {status.pc_connected
              ? `PC connected: ${status.pc_address} (${status.packets_per_second} packets/s)`
              : "Waiting for the PC on Wi-Fi"}
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>{`This Deck: ${status.deck_address || "address unknown"}, UDP port ${status.port}`}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>{status.message}</div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Network">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => scanNetwork()}>
            {status.scanning ? "Scanning..." : "Scan network"}
          </ButtonItem>
        </PanelSectionRow>
        {(status.scan_result ?? []).map((line, i) => (
          <PanelSectionRow key={`scan${i}`}>
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
    icon: <FaWifi />,
    onDismount() {},
  };
});
