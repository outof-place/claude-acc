import AppKit
import CoreGraphics
import Foundation

// Natywny pomocnik bramy pulpitu: robi to, czego Python nie zrobi dobrze (CGEvent, ScreenCaptureKit, AX).
// Mówi JSON-em: jedno żądanie w argv (`claude-acc-desktop '{"op":"screenshot"}'`) albo tryb `serve`
// (po jednym obiekcie JSON na linię na stdin, po jednej odpowiedzi na linię na stdout). Uprawnienia
// (Accessibility, Screen Recording) są przypięte do podpisu TEJ binarki, więc stabilny podpis trzyma zgodę.

// Współrzędne we wszystkich operacjach wejścia są w punktach układu globalnego: mapowanie z pikseli
// zrzutu robi Python i podaje tu gotowe punkty.

let DENY_OPS: Set<String> = ["screenshot", "zoom", "click", "mouse_down", "mouse_up", "drag", "scroll", "type", "key", "hold_key"]

func num(_ v: Any?) -> Double? {
    if let d = v as? Double { return d }
    if let i = v as? Int { return Double(i) }
    if let n = v as? NSNumber { return n.doubleValue }
    return nil
}

func point(_ req: [String: Any], _ kx: String = "x", _ ky: String = "y") -> CGPoint {
    CGPoint(x: num(req[kx]) ?? 0, y: num(req[ky]) ?? 0)
}

var denyBundles: Set<String> = []
var denyWindowMarks: [String] = []

/// Zwraca komunikat odmowy, gdy aplikacja na wierzchu jest na liście sekretów, inaczej nil.
func denied(for op: String) -> String? {
    guard DENY_OPS.contains(op) else { return nil }
    let front = AX.frontmost()
    if denyBundles.contains(front.bundle) {
        return "refused: \(front.bundle) is a protected app (secrets or permissions); it stays read-only"
    }
    // Ustawienia systemowe: blokujemy tylko panele hasła/prywatności
    if front.bundle == "com.apple.systempreferences" {
        let w = front.window.lowercased()
        if denyWindowMarks.contains(where: { w.contains($0) }) {
            return "refused: System Settings is on a protected pane (\(front.window)); it stays read-only"
        }
    }
    return nil
}

func shotJSON(_ s: Screen.Shot) -> [String: Any] {
    [
        "png": s.png.base64EncodedString(),
        "px_w": s.pxW, "px_h": s.pxH,
        "origin_x": s.originX, "origin_y": s.originY,
        "pt_w": s.ptW, "pt_h": s.ptH, "display": Int(s.display),
    ]
}

func handle(_ req: [String: Any]) -> [String: Any] {
    if let d = req["deny"] as? [String] { denyBundles = Set(d) }
    if let d = req["deny_window"] as? [String] { denyWindowMarks = d.map { $0.lowercased() } }
    let op = (req["op"] as? String) ?? ""
    if op == "init" { return ["ok": true] }

    if let msg = denied(for: op) { return ["ok": false, "error": msg, "protected": true] }

    do {
        switch op {
        case "probe":
            let front = AX.frontmost()
            return [
                "ok": true,
                "ax": AX.trusted(),
                "screen": CGPreflightScreenCaptureAccess(),
                "post": CGPreflightPostEventAccess(),
                "path": Bundle.main.executablePath ?? CommandLine.arguments[0],
                "displays": Screen.displays().map {
                    ["id": Int($0.id), "x": $0.x, "y": $0.y, "w": $0.w, "h": $0.h, "scale": $0.scale, "main": $0.main]
                },
                "frontmost": ["bundle": front.bundle, "window": front.window],
            ]
        case "screenshot":
            let s = try Screen.capture(display: UInt32(num(req["display"]) ?? 0), maxPx: Int(num(req["max_px"]) ?? 1600))
            var out = shotJSON(s); out["ok"] = true; return out
        case "zoom":
            let s = try Screen.captureRegion(x: num(req["x"]) ?? 0, y: num(req["y"]) ?? 0,
                                             w: num(req["w"]) ?? 0, h: num(req["h"]) ?? 0,
                                             maxPx: Int(num(req["max_px"]) ?? 1600))
            var out = shotJSON(s); out["ok"] = true; return out
        case "move":
            Input.move(to: point(req)); return ["ok": true]
        case "click":
            let button: CGMouseButton = {
                switch req["button"] as? String { case "right": return .right; case "middle": return .center; default: return .left }
            }()
            Input.click(point(req), button: button, count: Int(num(req["count"]) ?? 1), modifiers: Input.flags(req["modifiers"] as? String))
            return ["ok": true]
        case "mouse_down":
            Input.mouseDown(point(req), modifiers: Input.flags(req["modifiers"] as? String)); return ["ok": true]
        case "mouse_up":
            Input.mouseUp(point(req), modifiers: Input.flags(req["modifiers"] as? String)); return ["ok": true]
        case "drag":
            Input.drag(from: point(req), to: point(req, "x2", "y2"), modifiers: Input.flags(req["modifiers"] as? String)); return ["ok": true]
        case "scroll":
            Input.scroll(point(req), dx: Int32(num(req["dx"]) ?? 0), dy: Int32(num(req["dy"]) ?? 0),
                         modifiers: Input.flags(req["modifiers"] as? String)); return ["ok": true]
        case "type":
            Input.type((req["text"] as? String) ?? ""); return ["ok": true]
        case "key":
            try Input.keyChord((req["text"] as? String) ?? "", repeatCount: Int(num(req["repeat"]) ?? 1)); return ["ok": true]
        case "hold_key":
            try Input.holdKey((req["text"] as? String) ?? "", duration: num(req["duration"]) ?? 0); return ["ok": true]
        case "cursor_position":
            let p = Input.cursorPosition(); return ["ok": true, "x": p.x, "y": p.y]
        case "frontmost":
            let f = AX.frontmost(); return ["ok": true, "bundle": f.bundle, "window": f.window]
        case "ax_allow":
            let r = AX.allowRemoteDebugging(pid: pid_t(num(req["pid"]) ?? 0))
            return ["ok": true, "sheets": r.sheets, "pressed": r.pressed]
        default:
            return ["ok": false, "error": "unknown op: \(op)"]
        }
    } catch let e as Input.KeyError {
        if case let .unknown(name) = e { return ["ok": false, "error": "unknown key: \(name)"] }
        return ["ok": false, "error": "key error"]
    } catch let e as Err {
        return ["ok": false, "error": e.text]
    } catch {
        return ["ok": false, "error": "\(error)"]
    }
}

func emit(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj) else {
        FileHandle.standardOutput.write(Data(#"{"ok":false,"error":"encode"}"#.utf8) + Data("\n".utf8)); return
    }
    FileHandle.standardOutput.write(data + Data("\n".utf8))
}

let args = Array(CommandLine.arguments.dropFirst())
if args.first == "serve" {
    while let line = readLine(strippingNewline: true) {
        if line.isEmpty { continue }
        guard let data = line.data(using: .utf8),
              let req = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            emit(["ok": false, "error": "bad json"]); continue
        }
        emit(handle(req))
    }
} else if let json = args.first, let data = json.data(using: .utf8),
          let req = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
    let result = handle(req)
    emit(result)
    exit((result["ok"] as? Bool) == false ? 1 : 0)
} else {
    emit(["ok": false, "error": "usage: claude-acc-desktop serve | '<json>'"])
    exit(2)
}
