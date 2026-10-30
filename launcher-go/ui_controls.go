package main

import (
	"math"
	"strconv"
	"strings"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

type numberRange struct {
	min, max, step float64
	decimals       int
}

func numericRange(mode, key string) (numberRange, bool) {
	limits := numberRange{min: 1, step: 1}
	switch key {
	case "card":
		limits.max = 999999999
		if mode == "daily" {
			limits.max = 2147483647
		}
	case "worker_silence", "business_silence":
		limits.max = 86400
	case "max_attempts":
		limits.max = 100
		if mode == "daily" {
			limits.max = 20
		}
	case "worker_count":
		limits.max = 20
	case "default_offset", "offset":
		limits.min, limits.max = 0, 365
	case "max_rebuilds":
		limits.min, limits.max = 0, 100
	case "chunk_days":
		limits.max = 366
	case "session_hours":
		limits = numberRange{min: .1, max: 168, step: .1, decimals: 1}
	default:
		return numberRange{}, false
	}
	return limits, true
}

// 一个 Entry 右侧嵌入上下箭头，不改变原有表单读写方式。
type numberArrows struct {
	widget.BaseWidget
	entry    *widget.Entry
	limits   numberRange
	up, down *widget.Button
}

func newNumberArrows(entry *widget.Entry, limits numberRange) *numberArrows {
	a := &numberArrows{entry: entry, limits: limits}
	a.ExtendBaseWidget(a)
	a.up = widget.NewButtonWithIcon("", theme.MenuDropUpIcon(), func() { a.adjust(1) })
	a.down = widget.NewButtonWithIcon("", theme.MenuDropDownIcon(), func() { a.adjust(-1) })
	a.up.Importance, a.down.Importance = widget.LowImportance, widget.LowImportance
	entry.ActionItem = a
	entry.OnChanged = func(string) { a.Refresh() }
	a.Refresh()
	return a
}

func (a *numberArrows) value() (float64, bool) {
	value, err := strconv.ParseFloat(strings.TrimSpace(a.entry.Text), 64)
	valid := err == nil && !math.IsNaN(value) && !math.IsInf(value, 0) && value >= a.limits.min && value <= a.limits.max
	if a.limits.decimals == 0 {
		valid = valid && value == math.Trunc(value)
	}
	return value, valid
}

func (a *numberArrows) adjust(direction float64) {
	value, valid := a.value()
	if !valid || a.entry.Disabled() {
		return
	}
	scale := math.Pow10(a.limits.decimals)
	value = math.Round((value+direction*a.limits.step)*scale) / scale
	value = max(a.limits.min, min(a.limits.max, value))
	a.entry.SetText(strconv.FormatFloat(value, 'f', a.limits.decimals, 64))
}

func (a *numberArrows) Refresh() {
	value, valid := a.value()
	for button, enabled := range map[*widget.Button]bool{
		a.up:   valid && !a.entry.Disabled() && value < a.limits.max,
		a.down: valid && !a.entry.Disabled() && value > a.limits.min,
	} {
		if enabled {
			button.Enable()
		} else {
			button.Disable()
		}
	}
	a.BaseWidget.Refresh()
}

func (a *numberArrows) CreateRenderer() fyne.WidgetRenderer {
	compact := &arrowTheme{Theme: a.Theme()}
	buttons := container.New(arrowLayout{}, container.NewThemeOverride(a.up, compact), container.NewThemeOverride(a.down, compact))
	return widget.NewSimpleRenderer(container.NewBorder(nil, nil, widget.NewSeparator(), nil, buttons))
}

type arrowTheme struct{ fyne.Theme }

func (t *arrowTheme) Size(name fyne.ThemeSizeName) float32 {
	switch name {
	case theme.SizeNameInlineIcon:
		return 12
	case theme.SizeNameText:
		return 8
	case theme.SizeNameInnerPadding, theme.SizeNamePadding, theme.SizeNameButtonRadius:
		return 0
	default:
		return t.Theme.Size(name)
	}
}

type arrowLayout struct{}

func (arrowLayout) MinSize([]fyne.CanvasObject) fyne.Size { return fyne.NewSize(24, 16) }
func (arrowLayout) Layout(objects []fyne.CanvasObject, size fyne.Size) {
	for i, object := range objects {
		object.Move(fyne.NewPos(0, float32(i)*size.Height/2))
		object.Resize(fyne.NewSize(size.Width, size.Height/2))
	}
}

func (u *launcherUI) dateEntry(key string) *widget.Entry {
	entry := u.entry(key, "YYYY-MM-DD")
	if entry.ActionItem != nil {
		return entry
	}
	button := widget.NewButtonWithIcon("", theme.CalendarIcon(), func() {
		if entry.Disabled() {
			return
		}
		date, err := time.Parse("2006-01-02", entry.Text)
		if err != nil {
			date = time.Now().AddDate(0, 0, -1)
		}
		var popup *dialog.CustomDialog
		calendar := widget.NewCalendar(date, func(selected time.Time) {
			entry.SetText(selected.Format("2006-01-02"))
			popup.Hide()
		})
		popup = dialog.NewCustom("选择日期", "关闭", calendar, u.window)
		popup.Show()
	})
	button.Importance = widget.LowImportance
	entry.ActionItem = button
	return entry
}

// 使用虚拟化列表限制弹层高度，保留客户名输入及原有加载回调。
type customerEntry struct {
	widget.Entry
	options []string
	popup   *widget.PopUp
}

func newCustomerEntry(options []string) *customerEntry {
	e := &customerEntry{}
	e.ExtendBaseWidget(e)
	e.Wrapping = fyne.TextWrap(fyne.TextTruncateClip)
	button := widget.NewButtonWithIcon("", theme.MenuDropDownIcon(), e.showOptions)
	button.Importance = widget.LowImportance
	e.ActionItem = button
	e.SetOptions(options)
	return e
}

func (e *customerEntry) SetOptions(options []string) {
	if e.popup != nil {
		e.popup.Hide()
	}
	e.options = append([]string(nil), options...)
	e.updateButton()
}

func (e *customerEntry) updateButton() {
	button := e.ActionItem.(*widget.Button)
	if e.Disabled() || len(e.options) == 0 {
		button.Disable()
	} else {
		button.Enable()
	}
}

func (e *customerEntry) Disable() {
	if e.popup != nil {
		e.popup.Hide()
	}
	e.Entry.Disable()
	e.updateButton()
}

func (e *customerEntry) Enable() {
	e.Entry.Enable()
	e.updateButton()
}

func (e *customerEntry) Move(pos fyne.Position) {
	if e.popup != nil && pos != e.Position() {
		e.popup.Hide()
	}
	e.Entry.Move(pos)
}

func (e *customerEntry) Resize(size fyne.Size) {
	if e.popup != nil && size != e.Size() {
		e.popup.Hide()
	}
	e.Entry.Resize(size)
}

func (e *customerEntry) showOptions() {
	if e.Disabled() || len(e.options) == 0 {
		return
	}
	if e.popup != nil && e.popup.Visible() {
		e.popup.Hide()
		return
	}
	driver := fyne.CurrentApp().Driver()
	canvas := driver.CanvasForObject(e)
	if canvas == nil {
		return
	}
	createRow := func() fyne.CanvasObject {
		label := widget.NewLabel("")
		label.Truncation = fyne.TextTruncateEllipsis
		return inset(label, 4, 4)
	}
	list := widget.NewList(func() int { return len(e.options) }, createRow, func(id widget.ListItemID, object fyne.CanvasObject) {
		object.(*fyne.Container).Objects[0].(*widget.Label).SetText(e.options[id])
	})
	rowHeight := createRow().MinSize().Height
	rowGap := list.Theme().Size(theme.SizeNamePadding)
	areaPos, areaSize := canvas.InteractiveArea()
	pos := driver.AbsolutePositionForObject(e)
	below := max(float32(0), areaPos.Y+areaSize.Height-pos.Y-e.Size().Height-4)
	above := max(float32(0), pos.Y-areaPos.Y-4)
	rows := float32(min(8, len(e.options)))
	height := max(rowHeight*rows+rowGap*(rows-1), list.MinSize().Height)
	y := pos.Y + e.Size().Height
	if below < height && above > below {
		height = min(height, above)
		y = pos.Y - height
	} else {
		height = min(height, below)
	}
	if height < list.MinSize().Height {
		return
	}
	width := min(e.Size().Width, areaSize.Width)
	x := max(areaPos.X, min(pos.X, areaPos.X+areaSize.Width-width))
	e.popup = widget.NewPopUp(list, canvas)
	list.OnSelected = func(id widget.ListItemID) {
		e.popup.Hide()
		e.SetText(e.options[id])
		canvas.Focus(e)
	}
	e.popup.Resize(fyne.NewSize(width, height))
	e.popup.ShowAtPosition(fyne.NewPos(x, y))
	for id, option := range e.options {
		if option == e.Text {
			list.ScrollTo(id)
			break
		}
	}
}
