#!/usr/bin/env swift
//
// Renders the Venvy app icon to a 1024×1024 PNG.
//
// Redraws the mark from the web frontend's favicon (a dark rounded square with
// a blue-gradient "V") with Core Graphics, so the icon stays reproducible
// without checking in a binary or depending on an SVG rasteriser.
//
//   swift ios/Tools/make-appicon.swift ios/Venvy/Assets.xcassets/AppIcon.appiconset/AppIcon.png
//
import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let size = 1024.0
let out = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "AppIcon.png"

let space = CGColorSpaceCreateDeviceRGB()
guard let ctx = CGContext(data: nil, width: Int(size), height: Int(size),
                          bitsPerComponent: 8, bytesPerRow: 0, space: space,
                          bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
    fatalError("Couldn't create the drawing context.")
}

// Core Graphics has its origin bottom-left; the source artwork is described
// top-down, so flip once and work in the SVG's coordinate space.
ctx.translateBy(x: 0, y: size)
ctx.scaleBy(x: 1, y: -1)
let s = size / 100.0   // artwork is authored on a 100×100 grid

// Background: the app's dark surface colour, filling the full tile. iOS masks
// the corners itself, so this is a plain square rather than a rounded rect.
ctx.setFillColor(CGColor(red: 0x0c/255.0, green: 0x11/255.0, blue: 0x1c/255.0, alpha: 1))
ctx.fill(CGRect(x: 0, y: 0, width: size, height: size))

// The "V" mark, matching the favicon path.
let path = CGMutablePath()
path.move(to: CGPoint(x: 28 * s, y: 32 * s))
path.addLine(to: CGPoint(x: 46 * s, y: 72 * s))
path.addCurve(to: CGPoint(x: 54 * s, y: 72 * s),
              control1: CGPoint(x: 48 * s, y: 76 * s),
              control2: CGPoint(x: 52 * s, y: 76 * s))
path.addLine(to: CGPoint(x: 72 * s, y: 32 * s))

ctx.saveGState()
ctx.addPath(path)
ctx.setLineWidth(14 * s)
ctx.setLineCap(.round)
ctx.setLineJoin(.round)
// Turn the stroke into a fillable region, then clip the gradient to it — the
// mark is a gradient-stroked path, which CG can't draw in one step.
ctx.replacePathWithStrokedPath()
ctx.clip()

let colors = [
    CGColor(red: 0x3b/255.0, green: 0x82/255.0, blue: 0xf6/255.0, alpha: 1),
    CGColor(red: 0x25/255.0, green: 0x63/255.0, blue: 0xeb/255.0, alpha: 1),
] as CFArray
guard let gradient = CGGradient(colorsSpace: space, colors: colors, locations: [0, 1]) else {
    fatalError("Couldn't build the gradient.")
}
ctx.drawLinearGradient(gradient,
                       start: CGPoint(x: 0, y: 0),
                       end: CGPoint(x: size, y: size),
                       options: [])
ctx.restoreGState()

guard let image = ctx.makeImage() else { fatalError("Couldn't render the image.") }
let url = URL(fileURLWithPath: out)
guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
    fatalError("Couldn't open \(out) for writing.")
}
CGImageDestinationAddImage(dest, image, nil)
guard CGImageDestinationFinalize(dest) else { fatalError("Couldn't write \(out).") }
print("Wrote \(out) (\(Int(size))×\(Int(size)))")
