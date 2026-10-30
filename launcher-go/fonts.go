package main

import (
	"encoding/binary"
	"fmt"
	"image/color"
	"log"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/theme"
	"github.com/go-text/typesetting/font"
	"github.com/go-text/typesetting/font/opentype"
)

type launcherTheme struct {
	fyne.Theme
	regular, bold fyne.Resource
}

func newLauncherTheme() fyne.Theme {
	base := theme.DefaultTheme()
	var paths []string
	family := ""
	switch runtime.GOOS {
	case "darwin":
		// 本机 PingFangUI 是 CoreText 使用的 UI 字体，Fyne 直接读取时无法
		// 正常显示。选择已验证可渲染的系统无衬线黑体，不依赖私有字体路径。
		family = "Heiti SC"
		paths = []string{
			"/System/Library/Fonts/STHeiti Medium.ttc",
		}
	case "windows":
		family = "Microsoft YaHei UI"
		paths = []string{filepath.Join(os.Getenv("WINDIR"), "Fonts", "msyh.ttc")}
	}
	for _, path := range paths {
		regular, bold, err := loadSystemFontFamily(path, family)
		if err == nil {
			log.Printf("界面字体：%s（读取本机系统字体，不随应用分发）", family)
			return &launcherTheme{Theme: base, regular: regular, bold: bold}
		}
		if !os.IsNotExist(err) {
			log.Printf("读取界面字体失败，将尝试回退：%v", err)
		}
	}
	log.Print("未找到首选系统中文字体，保留 Fyne 默认字体回退。")
	return &launcherTheme{Theme: base}
}

func (t *launcherTheme) Font(style fyne.TextStyle) fyne.Resource {
	if style.Monospace || style.Symbol || t.regular == nil {
		return t.Theme.Font(style)
	}
	if style.Bold {
		return t.bold
	}
	return t.regular
}

func (t *launcherTheme) Color(name fyne.ThemeColorName, variant fyne.ThemeVariant) color.Color {
	light, dark := uint32(0), uint32(0)
	switch name {
	case theme.ColorNameBackground, theme.ColorNameDisabledButton:
		light, dark = 0xf4f3ee, 0x20231f
	case theme.ColorNameButton, theme.ColorNameInputBackground, theme.ColorNameMenuBackground, theme.ColorNameOverlayBackground:
		light, dark = 0xfcfbf8, 0x272b25
	case theme.ColorNameForeground:
		light, dark = 0x262923, 0xedece5
	case theme.ColorNamePlaceHolder, theme.ColorNameDisabled:
		light, dark = 0x62675c, 0xb9c0b2
	case theme.ColorNameInputBorder:
		light, dark = 0x858c7d, 0x7f8b75
	case theme.ColorNameSeparator:
		light, dark = 0xd6d9ce, 0x4d5547
	case theme.ColorNamePrimary:
		light, dark = 0xeca66d, 0xe8aa79
	case theme.ColorNameForegroundOnPrimary:
		light, dark = 0x2b2118, 0x2b2118
	case theme.ColorNameSelection, theme.ColorNamePressed:
		light, dark = 0xe8ecdf, 0x394331
	case theme.ColorNameHover, theme.ColorNameHeaderBackground:
		light, dark = 0xefeee7, 0x353c2f
	case theme.ColorNameFocus:
		if variant == theme.VariantDark {
			return color.NRGBA{R: 245, G: 189, B: 145, A: 100}
		}
		return color.NRGBA{R: 146, G: 80, B: 28, A: 100}
	case theme.ColorNameShadow:
		return color.Transparent
	default:
		return t.Theme.Color(name, variant)
	}
	value := light
	if variant == theme.VariantDark {
		value = dark
	}
	return color.NRGBA{R: byte(value >> 16), G: byte(value >> 8), B: byte(value), A: 255}
}

func (t *launcherTheme) Size(name fyne.ThemeSizeName) float32 {
	switch name {
	case theme.SizeNameText:
		return 14
	case theme.SizeNameCaptionText:
		return 12
	case theme.SizeNameHeadingText:
		return 24
	case theme.SizeNameSubHeadingText:
		return 18
	case theme.SizeNameInputRadius, theme.SizeNameButtonRadius:
		return 6
	case theme.SizeNameInputBorder, theme.SizeNameSeparatorThickness:
		return 1
	case theme.SizeNamePadding:
		return 4
	case theme.SizeNameInnerPadding:
		return 8
	case theme.SizeNameSplitThickness:
		return 6
	default:
		return t.Theme.Size(name)
	}
}

func loadSystemFontFamily(path, family string) (fyne.Resource, fyne.Resource, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}
	defer file.Close()
	loaders, err := opentype.NewLoaders(file)
	if err != nil {
		return nil, nil, err
	}
	var selected [2]*opentype.Loader
	scores := [2]float64{math.Inf(1), math.Inf(1)}
	for _, loader := range loaders {
		description, _ := font.Describe(loader, nil)
		if !strings.EqualFold(description.Family, family) || description.Aspect.Style != font.StyleNormal {
			continue
		}
		for index, weight := range []float64{400, 600} {
			score := math.Abs(float64(description.Aspect.Weight) - weight)
			if score < scores[index] {
				selected[index], scores[index] = loader, score
			}
		}
	}
	if selected[0] == nil {
		return nil, nil, fmt.Errorf("%s 中未找到字体族 %s", path, family)
	}
	regular, err := singleFontResource(selected[0], family+"-Regular.ttf")
	if err != nil {
		return nil, nil, err
	}
	if selected[1] == selected[0] {
		return regular, regular, nil
	}
	bold, err := singleFontResource(selected[1], family+"-Semibold.ttf")
	return regular, bold, err
}

// Fyne 的主题资源读取单个 SFNT 字体。系统 TTC 是字体集合，需先在内存中
// 取出选中的字重，重建表目录/偏移与校验和；不会写出或打包系统字体文件。
func singleFontResource(loader *opentype.Loader, name string) (fyne.Resource, error) {
	tags := loader.Tables()
	data := make([]byte, 12+16*len(tags))
	binary.BigEndian.PutUint32(data, uint32(loader.Type))
	binary.BigEndian.PutUint16(data[4:], uint16(len(tags)))
	var power, selector uint16 = 1, 0
	for int(power)*2 <= len(tags) {
		power *= 2
		selector++
	}
	binary.BigEndian.PutUint16(data[6:], power*16)
	binary.BigEndian.PutUint16(data[8:], selector)
	binary.BigEndian.PutUint16(data[10:], uint16(len(tags))*16-power*16)
	headOffset := -1
	for index, tag := range tags {
		table, err := loader.RawTable(tag)
		if err != nil {
			return nil, err
		}
		offset := len(data)
		data = append(data, table...)
		for len(data)%4 != 0 {
			data = append(data, 0)
		}
		if tag == opentype.NewTag('h', 'e', 'a', 'd') {
			if len(table) < 12 {
				return nil, fmt.Errorf("字体 head 表过短")
			}
			headOffset = offset
			binary.BigEndian.PutUint32(data[offset+8:], 0)
		}
		record := data[12+index*16:]
		binary.BigEndian.PutUint32(record, uint32(tag))
		binary.BigEndian.PutUint32(record[4:], fontChecksum(data[offset:]))
		binary.BigEndian.PutUint32(record[8:], uint32(offset))
		binary.BigEndian.PutUint32(record[12:], uint32(len(table)))
	}
	if headOffset >= 0 {
		binary.BigEndian.PutUint32(data[headOffset+8:], 0xb1b0afba-fontChecksum(data))
	}
	return fyne.NewStaticResource(name, data), nil
}

func fontChecksum(data []byte) uint32 {
	var sum uint32
	for index := 0; index+4 <= len(data); index += 4 {
		sum += binary.BigEndian.Uint32(data[index:])
	}
	return sum
}
