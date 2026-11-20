import React, { useEffect, useState } from "react";
import { ButtonItem, PanelSection, PanelSectionRow, staticClasses } from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { FaWifi } from "react-icons/fa";

type Status = {
  version: string;
  pad_ok: boolean;
  pad_message: string;
  link_state: string; // "idle" | "connected" | "refused" | "off"
  pc_address: string;
  pairing_code: string;
  deck_address: string;
  port: number;
  scanning: boolean;
  scan_result: string[];
  message: string;
};

const getStatus = callable<[], Status>("status");
const scanNetwork = callable<[], boolean>("scan");
const disconnectPc = callable<[], boolean>("disconnect");
const connectPc = callable<[], boolean>("connect");
const newPairingCode = callable<[], boolean>("new_pairing_code");

function linkLine(s: Status): string {
  if (s.link_state === "connected") return `Connected to the PC at ${s.pc_address}`;
  if (s.link_state === "refused") return "Disconnected by you. Press Connect to accept the PC again.";
  return "Waiting for the PC. Enter this pairing code on the PC.";
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
          <div>{error ? `Backend error: ${error}` : "Starting..."}</div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const refused = status.link_state === "refused";
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
          <div>{linkLine(status)}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>{`This Deck: ${status.deck_address || "address unknown"}, UDP port ${status.port}`}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div>{status.message}</div>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Connection">
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => (refused ? connectPc() : disconnectPc())}>
            {refused ? "Connect to PC" : "Disconnect from PC"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Pairing">
        <PanelSectionRow>
          <div>{`Pairing code: ${status.pairing_code || "(none yet)"}`}</div>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => newPairingCode()}>
            New pairing code
          </ButtonItem>
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
