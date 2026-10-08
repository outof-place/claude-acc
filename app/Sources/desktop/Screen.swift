import AppKit
import CoreGraphics
import ScreenCaptureKit

/// Zrzuty ekranu przez ScreenCaptureKit (uprawnienie Screen Recording przypięte do podpisu tej binarki).
/// Pełny ekran skalujemy do rozmiaru logicznego (Retina 2x -> 1x), z twardym limitem dłuższej krawędzi,
/// żeby piksel zrzutu odpowiadał punktowi układu globalnego: współrzędne modelu mapują się wtedy wprost.
enum Screen {
    struct Shot {
        let png: Data
        let pxW: Int
        let pxH: Int
        // ramka wyświetlacza w punktach globalnych (lewy górny róg to origin)
        let originX: Double
        let originY: Double
        let ptW: Double
        let ptH: Double
        let display: UInt32
    }

    struct DisplayInfo {
        let id: UInt32
        let x: Double
        let y: Double
        let w: Double
        let h: Double
        let scale: Double
        let main: Bool
    }

    static func displays() -> [DisplayInfo] {
        NSScreen.screens.compactMap { screen in
            guard let num = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? UInt32 else { return nil }
            // NSScreen.frame ma origin w lewym DOLNYM rogu globalnie; CG używa lewego górnego.
            // CGDisplayBounds daje od razu układ CG (top-left), którego używają CGEvent i SCDisplay.
            let b = CGDisplayBounds(num)
            return DisplayInfo(id: num, x: b.origin.x, y: b.origin.y, w: b.width, h: b.height,
                               scale: screen.backingScaleFactor, main: b.origin == .zero)
        }
    }

    /// Łapie SCDisplay odpowiadający CGDirectDisplayID (albo główny, gdy id==0).
    private static func shareable() throws -> SCShareableContent {
        let sem = DispatchSemaphore(value: 0)
        var result: SCShareableContent?
        var failure: Error?
        SCShareableContent.getWithCompletionHandler { content, error in
            result = content; failure = error; sem.signal()
        }
        if sem.wait(timeout: .now() + 8) == .timedOut { throw Err.msg("screen content timed out (grant Screen Recording?)") }
        if let failure { throw failure }
        guard let result else { throw Err.msg("no screen content") }
        return result
    }

    static func capture(display wanted: UInt32, maxPx: Int) throws -> Shot {
        let content = try shareable()
        let target: SCDisplay
        if wanted == 0 {
            guard let main = content.displays.first(where: { CGDisplayBounds($0.displayID).origin == .zero }) ?? content.displays.first
            else { throw Err.msg("no display") }
            target = main
        } else {
            guard let d = content.displays.first(where: { $0.displayID == wanted }) else { throw Err.msg("no display \(wanted)") }
            target = d
        }
        let b = CGDisplayBounds(target.displayID)
        // rozmiar wyjściowy: logiczny (punkty), przycięty do maxPx na dłuższej krawędzi
        let longEdge = max(b.width, b.height)
        let cap = Double(maxPx)
        let factor = longEdge > cap ? cap / longEdge : 1.0
        let outW = max(1, Int((b.width * factor).rounded()))
        let outH = max(1, Int((b.height * factor).rounded()))
        let filter = SCContentFilter(display: target, excludingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.width = outW
        cfg.height = outH
        cfg.showsCursor = true
        cfg.ignoreShadowsDisplay = true
        let image = try captureImage(filter: filter, cfg: cfg)
        return Shot(png: try png(image), pxW: image.width, pxH: image.height,
                    originX: b.origin.x, originY: b.origin.y, ptW: b.width, ptH: b.height, display: target.displayID)
    }

    /// Wycinek w punktach globalnych, w pełnej rozdzielczości, przeskalowany tak, by zmieścił się w maxPx.
    static func captureRegion(x: Double, y: Double, w: Double, h: Double, maxPx: Int) throws -> Shot {
        guard w > 0, h > 0 else { throw Err.msg("empty region") }
        let rect = CGRect(x: x, y: y, width: w, height: h)
        let sem = DispatchSemaphore(value: 0)
        var out: CGImage?
        var failure: Error?
        SCScreenshotManager.captureImage(in: rect) { image, error in out = image; failure = error; sem.signal() }
        if sem.wait(timeout: .now() + 8) == .timedOut { throw Err.msg("zoom timed out") }
        if let failure { throw failure }
        guard var image = out else { throw Err.msg("no zoom image") }
        // dociągnij do maxPx na dłuższej krawędzi (w górę dla małych, w dół dla dużych)
        let longEdge = max(image.width, image.height)
        if longEdge != maxPx, longEdge > 0 {
            let f = Double(maxPx) / Double(longEdge)
            image = resize(image, w: max(1, Int(Double(image.width) * f)), h: max(1, Int(Double(image.height) * f))) ?? image
        }
        return Shot(png: try png(image), pxW: image.width, pxH: image.height,
                    originX: x, originY: y, ptW: w, ptH: h, display: 0)
    }

    private static func captureImage(filter: SCContentFilter, cfg: SCStreamConfiguration) throws -> CGImage {
        let sem = DispatchSemaphore(value: 0)
        var out: CGImage?
        var failure: Error?
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg) { image, error in
            out = image; failure = error; sem.signal()
        }
        if sem.wait(timeout: .now() + 8) == .timedOut { throw Err.msg("screenshot timed out") }
        if let failure { throw failure }
        guard let out else { throw Err.msg("no screenshot") }
        return out
    }

    private static func resize(_ image: CGImage, w: Int, h: Int) -> CGImage? {
        guard let space = image.colorSpace ?? CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return ctx.makeImage()
    }

    private static func png(_ image: CGImage) throws -> Data {
        let rep = NSBitmapImageRep(cgImage: image)
        guard let data = rep.representation(using: .png, properties: [:]) else { throw Err.msg("png encode failed") }
        return data
    }
}

struct Err: Error { let text: String; static func msg(_ t: String) -> Err { Err(text: t) } }
