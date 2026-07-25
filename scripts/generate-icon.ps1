Add-Type -AssemblyName System.Drawing

function Add-RoundedRectangle {
  param(
    [System.Drawing.Drawing2D.GraphicsPath]$Path,
    [float]$X,
    [float]$Y,
    [float]$Width,
    [float]$Height,
    [float]$Radius
  )

  $diameter = $Radius * 2
  $Path.AddArc($X, $Y, $diameter, $diameter, 180, 90)
  $Path.AddArc($X + $Width - $diameter, $Y, $diameter, $diameter, 270, 90)
  $Path.AddArc($X + $Width - $diameter, $Y + $Height - $diameter, $diameter, $diameter, 0, 90)
  $Path.AddArc($X, $Y + $Height - $diameter, $diameter, $diameter, 90, 90)
  $Path.CloseFigure()
}

$outputDirectory = Join-Path $PSScriptRoot "..\build"
$outputPath = Join-Path $outputDirectory "icon.png"
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

$bitmap = New-Object System.Drawing.Bitmap 512, 512
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([System.Drawing.Color]::FromArgb(16, 33, 27))

$lanternPath = New-Object System.Drawing.Drawing2D.GraphicsPath
Add-RoundedRectangle -Path $lanternPath -X 92 -Y 82 -Width 328 -Height 348 -Radius 92
$amberBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(240, 166, 64))
$inkBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(16, 33, 27))
$graphics.FillPath($amberBrush, $lanternPath)

$graphics.FillRectangle($inkBrush, 152, 273, 52, 91)
$graphics.FillRectangle($inkBrush, 230, 151, 52, 213)
$graphics.FillRectangle($inkBrush, 308, 221, 52, 143)

$bitmap.Save($outputPath, [System.Drawing.Imaging.ImageFormat]::Png)

$inkBrush.Dispose()
$amberBrush.Dispose()
$lanternPath.Dispose()
$graphics.Dispose()
$bitmap.Dispose()
