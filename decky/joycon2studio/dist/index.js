const manifest = {"name":"Joy-Con 2 Studio"};
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error('[@decky/api]: Failed to connect to the loader as as the loader API was not initialized. This is likely a bug in Decky Loader.');
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
}
catch {
    api = internalAPIConnection.connect(1, manifest.name);
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version 1. Some features may not work.`);
}
if (api._version != API_VERSION) {
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version ${api._version}. Some features may not work.`);
}
const callable = api.callable;
const definePlugin = (fn) => {
    return (...args) => {
        return fn(...args);
    };
};

var DefaultContext = {
  color: undefined,
  size: undefined,
  className: undefined,
  style: undefined,
  attr: undefined
};
var IconContext = SP_REACT.createContext && /*#__PURE__*/SP_REACT.createContext(DefaultContext);

var _excluded = ["attr", "size", "title"];
function _objectWithoutProperties(e, t) { if (null == e) return {}; var o, r, i = _objectWithoutPropertiesLoose(e, t); if (Object.getOwnPropertySymbols) { var n = Object.getOwnPropertySymbols(e); for (r = 0; r < n.length; r++) o = n[r], -1 === t.indexOf(o) && {}.propertyIsEnumerable.call(e, o) && (i[o] = e[o]); } return i; }
function _objectWithoutPropertiesLoose(r, e) { if (null == r) return {}; var t = {}; for (var n in r) if ({}.hasOwnProperty.call(r, n)) { if (-1 !== e.indexOf(n)) continue; t[n] = r[n]; } return t; }
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function ownKeys(e, r) { var t = Object.keys(e); if (Object.getOwnPropertySymbols) { var o = Object.getOwnPropertySymbols(e); r && (o = o.filter(function (r) { return Object.getOwnPropertyDescriptor(e, r).enumerable; })), t.push.apply(t, o); } return t; }
function _objectSpread(e) { for (var r = 1; r < arguments.length; r++) { var t = null != arguments[r] ? arguments[r] : {}; r % 2 ? ownKeys(Object(t), true).forEach(function (r) { _defineProperty(e, r, t[r]); }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function (r) { Object.defineProperty(e, r, Object.getOwnPropertyDescriptor(t, r)); }); } return e; }
function _defineProperty(e, r, t) { return (r = _toPropertyKey(r)) in e ? Object.defineProperty(e, r, { value: t, enumerable: true, configurable: true, writable: true }) : e[r] = t, e; }
function _toPropertyKey(t) { var i = _toPrimitive(t, "string"); return "symbol" == typeof i ? i : i + ""; }
function _toPrimitive(t, r) { if ("object" != typeof t || !t) return t; var e = t[Symbol.toPrimitive]; if (void 0 !== e) { var i = e.call(t, r); if ("object" != typeof i) return i; throw new TypeError("@@toPrimitive must return a primitive value."); } return ("string" === r ? String : Number)(t); }
function Tree2Element(tree) {
  return tree && tree.map((node, i) => /*#__PURE__*/SP_REACT.createElement(node.tag, _objectSpread({
    key: i
  }, node.attr), Tree2Element(node.child)));
}
function GenIcon(data) {
  return props => /*#__PURE__*/SP_REACT.createElement(IconBase, _extends({
    attr: _objectSpread({}, data.attr)
  }, props), Tree2Element(data.child));
}
function IconBase(props) {
  var elem = conf => {
    var attr = props.attr,
      size = props.size,
      title = props.title,
      svgProps = _objectWithoutProperties(props, _excluded);
    var computedSize = size || conf.size || "1em";
    var className;
    if (conf.className) className = conf.className;
    if (props.className) className = (className ? className + " " : "") + props.className;
    return /*#__PURE__*/SP_REACT.createElement("svg", _extends({
      stroke: "currentColor",
      fill: "currentColor",
      strokeWidth: "0"
    }, conf.attr, attr, svgProps, {
      className: className,
      style: _objectSpread(_objectSpread({
        color: props.color || conf.color
      }, conf.style), props.style),
      height: computedSize,
      width: computedSize,
      xmlns: "http://www.w3.org/2000/svg"
    }), title && /*#__PURE__*/SP_REACT.createElement("title", null, title), props.children);
  };
  return IconContext !== undefined ? /*#__PURE__*/SP_REACT.createElement(IconContext.Consumer, null, conf => elem(conf)) : elem(DefaultContext);
}

// THIS FILE IS AUTO GENERATED
function FaGamepad (props) {
  return GenIcon({"attr":{"viewBox":"0 0 640 512"},"child":[{"tag":"path","attr":{"d":"M480.07 96H160a160 160 0 1 0 114.24 272h91.52A160 160 0 1 0 480.07 96zM248 268a12 12 0 0 1-12 12h-52v52a12 12 0 0 1-12 12h-24a12 12 0 0 1-12-12v-52H84a12 12 0 0 1-12-12v-24a12 12 0 0 1 12-12h52v-52a12 12 0 0 1 12-12h24a12 12 0 0 1 12 12v52h52a12 12 0 0 1 12 12zm216 76a40 40 0 1 1 40-40 40 40 0 0 1-40 40zm64-96a40 40 0 1 1 40-40 40 40 0 0 1-40 40z"},"child":[]}]})(props);
}

const getStatus = callable("status");
const startScan = callable("scan");
const disconnectAll = callable("disconnect_all");
const calibrate = callable("calibrate");
const testRumble = callable("test_rumble");
const playSong = callable("play_song");
const stopVibration = callable("stop_vibration");
function sideLine(label, s) {
    if (!s || !s.connected)
        return `${label}: waiting`;
    const power = s.charging ? "charging" : "battery";
    return `${label}: connected, ${power} ${s.battery ?? 0}%`;
}
const Content = () => {
    const [status, setStatus] = SP_REACT.useState(null);
    const [error, setError] = SP_REACT.useState("");
    SP_REACT.useEffect(() => {
        let alive = true;
        const tick = async () => {
            try {
                const s = await getStatus();
                if (alive) {
                    setStatus(s);
                    setError("");
                }
            }
            catch (e) {
                if (alive)
                    setError(String(e));
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
        return (SP_REACT.createElement(DFL.PanelSection, { title: "Status" },
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement("div", null, error ? `Backend error: ${error}` : "Starting the plugin..."))));
    }
    return (SP_REACT.createElement(SP_REACT.Fragment, null,
        SP_REACT.createElement(DFL.PanelSection, { title: "Status" },
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement("div", null, status.pad_ok ? "Virtual DualShock 4: ready" : `Virtual controller: ${status.pad_message}`)),
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement("div", null, sideLine("Left", status.left))),
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement("div", null, sideLine("Right", status.right))),
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement("div", null, status.message))),
        SP_REACT.createElement(DFL.PanelSection, { title: "Joy-Cons" },
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement(DFL.ButtonItem, { layout: "below", onClick: () => startScan() }, "Scan for Joy-Cons")),
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement(DFL.ButtonItem, { layout: "below", onClick: () => calibrate() }, "Zero sticks and gyro")),
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement(DFL.ButtonItem, { layout: "below", onClick: () => disconnectAll() }, "Disconnect all"))),
        SP_REACT.createElement(DFL.PanelSection, { title: "Rumble" },
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement(DFL.ButtonItem, { layout: "below", onClick: () => testRumble("R") }, "Test rumble (right)")),
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement(DFL.ButtonItem, { layout: "below", onClick: () => testRumble("L") }, "Test rumble (left)")),
            SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement(DFL.ButtonItem, { layout: "below", onClick: () => stopVibration() }, "Stop vibration"))),
        SP_REACT.createElement(DFL.PanelSection, { title: "Songs" }, (status.songs ?? []).map((name, index) => (SP_REACT.createElement(DFL.PanelSectionRow, { key: name },
            SP_REACT.createElement(DFL.ButtonItem, { layout: "below", onClick: () => playSong(index) }, name))))),
        SP_REACT.createElement(DFL.PanelSection, { title: "Diagnostics" },
            (status.diag ?? []).map((line, i) => (SP_REACT.createElement(DFL.PanelSectionRow, { key: `diag${i}` },
                SP_REACT.createElement("div", null, line)))),
            status.bleak_error ? (SP_REACT.createElement(DFL.PanelSectionRow, null,
                SP_REACT.createElement("div", null, status.bleak_error))) : null,
            (status.seen ?? []).map((line, i) => (SP_REACT.createElement(DFL.PanelSectionRow, { key: `seen${i}` },
                SP_REACT.createElement("div", null,
                    "Seen: ",
                    line)))),
            (status.recent ?? []).map((line, i) => (SP_REACT.createElement(DFL.PanelSectionRow, { key: `recent${i}` },
                SP_REACT.createElement("div", null, line)))))));
};
var index = definePlugin(() => {
    return {
        name: "Joy-Con 2 Studio",
        titleView: SP_REACT.createElement("div", { className: DFL.staticClasses.Title }, "Joy-Con 2 Studio"),
        content: SP_REACT.createElement(Content, null),
        icon: SP_REACT.createElement(FaGamepad, null),
        onDismount() { },
    };
});

export { index as default };
//# sourceMappingURL=index.js.map
