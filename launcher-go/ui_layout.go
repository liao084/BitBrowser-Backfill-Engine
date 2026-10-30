package main

import (
	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

func inset(object fyne.CanvasObject, vertical, horizontal float32) *fyne.Container {
	return container.New(layout.NewCustomPaddedLayout(vertical, vertical, horizontal, horizontal), object)
}

func heading(text string, size fyne.ThemeSizeName) *widget.RichText {
	return widget.NewRichText(&widget.TextSegment{Text: text, Style: widget.RichTextStyle{SizeName: size, ColorName: theme.ColorNameForeground}})
}

func helperText(text string) *widget.RichText {
	label := widget.NewRichText(&widget.TextSegment{Text: text, Style: widget.RichTextStyle{SizeName: theme.SizeNameCaptionText, ColorName: theme.ColorNamePlaceHolder}})
	label.Wrapping = fyne.TextWrapWord
	return label
}

func field(label string, control fyne.CanvasObject) fyne.CanvasObject {
	return container.NewVBox(widget.NewLabel(label), control)
}

func section(title string, content fyne.CanvasObject) fyne.CanvasObject {
	return inset(container.NewVBox(heading(title, theme.SizeNameSubHeadingText), inset(content, 4, 0)), 8, 0)
}

// 两列字段使用固定 10px 间距；只负责布局，不参与表单状态。
type fieldGridLayout struct{}

func (fieldGridLayout) MinSize(objects []fyne.CanvasObject) fyne.Size {
	var width, height float32
	for i := 0; i < len(objects); i += 2 {
		row := objects[i].MinSize()
		width = max(width, row.Width)
		if i+1 < len(objects) {
			other := objects[i+1].MinSize()
			width = max(width, other.Width)
			row.Height = max(row.Height, other.Height)
		}
		if i > 0 {
			height += 10
		}
		height += row.Height
	}
	return fyne.NewSize(width*2+10, height)
}

func (fieldGridLayout) Layout(objects []fyne.CanvasObject, size fyne.Size) {
	width := (size.Width - 10) / 2
	var y float32
	for i := 0; i < len(objects); i += 2 {
		height := objects[i].MinSize().Height
		if i+1 < len(objects) {
			height = max(height, objects[i+1].MinSize().Height)
		}
		for col := 0; col < 2 && i+col < len(objects); col++ {
			objects[i+col].Move(fyne.NewPos(float32(col)*(width+10), y))
			objects[i+col].Resize(fyne.NewSize(width, height))
		}
		y += height + 10
	}
}

// 面板背景在主题切换时更新，内部不再叠放带阴影的卡片。
type themePanel struct {
	widget.BaseWidget
	content fyne.CanvasObject
	role    fyne.ThemeColorName
}

func panel(content fyne.CanvasObject, role fyne.ThemeColorName) fyne.CanvasObject {
	p := &themePanel{content: content, role: role}
	p.ExtendBaseWidget(p)
	return p
}

func (p *themePanel) CreateRenderer() fyne.WidgetRenderer {
	background := canvas.NewRectangle(p.Theme().Color(p.role, fyne.CurrentApp().Settings().ThemeVariant()))
	return &panelRenderer{WidgetRenderer: widget.NewSimpleRenderer(container.NewStack(background, p.content)), panel: p, background: background}
}

type panelRenderer struct {
	fyne.WidgetRenderer
	panel      *themePanel
	background *canvas.Rectangle
}

type taskTableLayout struct {
	table   *widget.Table
	columns int
	width   float32
}

func (l *taskTableLayout) MinSize([]fyne.CanvasObject) fyne.Size {
	return fyne.NewSize(320, 160)
}

func (l *taskTableLayout) Layout(_ []fyne.CanvasObject, size fyne.Size) {
	if l.width != size.Width {
		l.width = size.Width
		available := max(float32(0), size.Width-12)
		for col := 0; col < l.columns; col++ {
			width := available / float32(l.columns)
			if l.columns == 2 {
				width = available * .35
				if col == 1 {
					width = available * .65
				}
			}
			l.table.SetColumnWidth(col, width)
		}
	}
	l.table.Move(fyne.NewPos(0, 0))
	l.table.Resize(size)
}

func (r *panelRenderer) Refresh() {
	r.background.FillColor = r.panel.Theme().Color(r.panel.role, fyne.CurrentApp().Settings().ThemeVariant())
	r.WidgetRenderer.Refresh()
}
