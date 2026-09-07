import fs from 'node:fs/promises'
import path from 'node:path'
import sharp from 'sharp'

const source = path.resolve('public/icons/app-icon.svg')
const foregroundSource = path.resolve('public/icons/app-foreground.svg')
const targets = [
  ['public/icons/icon-192.png', 192],
  ['public/icons/icon-512.png', 512],
  ['public/icons/icon-maskable-512.png', 512],
  ['public/apple-touch-icon.png', 180],
]

await Promise.all(targets.map(async ([filename, size]) => {
  const output = path.resolve(filename)
  await fs.mkdir(path.dirname(output), { recursive: true })
  await sharp(source).resize(Number(size), Number(size)).png().toFile(output)
}))

const androidResourceRoot = path.resolve('android/app/src/main/res')
try {
  await fs.access(androidResourceRoot)
  const densities = [
    ['mdpi', 48, 108],
    ['hdpi', 72, 162],
    ['xhdpi', 96, 216],
    ['xxhdpi', 144, 324],
    ['xxxhdpi', 192, 432],
  ]
  await Promise.all(densities.flatMap(([density, iconSize, foregroundSize]) => {
    const directory = path.join(androidResourceRoot, `mipmap-${density}`)
    return [
      sharp(source).resize(Number(iconSize), Number(iconSize)).png().toFile(path.join(directory, 'ic_launcher.png')),
      sharp(source).resize(Number(iconSize), Number(iconSize)).png().toFile(path.join(directory, 'ic_launcher_round.png')),
      sharp(foregroundSource).resize(Number(foregroundSize), Number(foregroundSize)).png().toFile(path.join(directory, 'ic_launcher_foreground.png')),
    ]
  }))

  const splashes = [
    ['drawable/splash.png', 480, 320],
    ['drawable-land-mdpi/splash.png', 480, 320],
    ['drawable-land-hdpi/splash.png', 800, 480],
    ['drawable-land-xhdpi/splash.png', 1280, 720],
    ['drawable-land-xxhdpi/splash.png', 1600, 960],
    ['drawable-land-xxxhdpi/splash.png', 1920, 1280],
    ['drawable-port-mdpi/splash.png', 320, 480],
    ['drawable-port-hdpi/splash.png', 480, 800],
    ['drawable-port-xhdpi/splash.png', 720, 1280],
    ['drawable-port-xxhdpi/splash.png', 960, 1600],
    ['drawable-port-xxxhdpi/splash.png', 1280, 1920],
  ]
  await Promise.all(splashes.map(async ([filename, width, height]) => {
    const logoSize = Math.round(Math.min(Number(width), Number(height)) * 0.28)
    const logo = await sharp(source).resize(logoSize, logoSize).png().toBuffer()
    return sharp({ create: { width: Number(width), height: Number(height), channels: 3, background: '#f6f4ee' } })
      .composite([{ input: logo, gravity: 'center' }])
      .png()
      .toFile(path.join(androidResourceRoot, String(filename)))
  }))
} catch {
  // Android 项目尚未创建时，只生成 PWA 图标。
}

console.log(`已生成手机应用图标与启动图。`)
