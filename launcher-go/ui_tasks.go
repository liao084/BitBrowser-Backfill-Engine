package main

import (
	"fmt"
	"image/color"
	"strconv"
	"strings"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/canvas"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

type taskCell struct {
	widget.BaseWidget
	label      *widget.Label
	background *canvas.Rectangle
}

func (cell *taskCell) CreateRenderer() fyne.WidgetRenderer {
	return widget.NewSimpleRenderer(container.NewStack(cell.background, container.NewBorder(nil, widget.NewSeparator(), nil, nil, inset(cell.label, 6, 0))))
}

func (u *launcherUI) taskEditor() *fyne.Container {
	form := container.New(fieldGridLayout{}, field("任务卡片 ID", u.entry("card", "1")))
	if u.config.Mode == "backfill" {
		form.Add(field("区块天数 / 天", u.entry("chunk_days", "1")))
		form.Add(field("开始日期", u.dateEntry("start")))
		form.Add(field("结束日期", u.dateEntry("end")))
	} else {
		form.Add(field("该任务日期偏移 / 天", u.entry("offset", "1")))
	}
	add := widget.NewButtonWithIcon("新增任务", theme.ContentAddIcon(), func() { u.editTask(false) })
	update := widget.NewButtonWithIcon("更新选中", theme.ViewRefreshIcon(), func() { u.editTask(true) })
	remove := widget.NewButtonWithIcon("删除选中", theme.DeleteIcon(), func() {
		if u.selected < 0 || u.selected >= len(u.model.Tasks) {
			return
		}
		index := u.selected
		removed := u.model.Tasks[index]
		u.removedTask, u.removedIndex = &removed, index
		u.model.Tasks = append(u.model.Tasks[:index], u.model.Tasks[index+1:]...)
		u.refreshTasks()
		u.status.SetText("选中任务已删除。")
	})
	columns := 3
	if u.config.Mode == "backfill" {
		columns = 4
	}
	buttons := container.NewGridWithColumns(columns, add, update, remove)
	u.taskButtons = []*widget.Button{update, remove}
	if u.config.Mode == "backfill" {
		copyButton := widget.NewButtonWithIcon("复制到编辑区", theme.ContentCopyIcon(), func() {
			if u.selected < 0 || u.selected >= len(u.model.Tasks) {
				return
			}
			u.loadTask(u.model.Tasks[u.selected])
			u.selected = -1
			u.table.UnselectAll()
			u.syncTaskSelection()
			u.status.SetText("已复制到编辑区，修改后点击“新增任务”。")
		})
		buttons.Add(copyButton)
		u.taskButtons = append(u.taskButtons, copyButton)
	}
	content := container.NewVBox(container.NewBorder(nil, nil, heading("任务编辑", theme.SizeNameSubHeadingText), u.editorState), form)
	if u.config.Mode == "backfill" {
		content.Add(u.latest)
	}
	content.Add(inset(buttons, 8, 0))
	u.syncTaskSelection()
	return content
}

func (u *launcherUI) buildTaskTable() {
	headers := []string{"卡片 ID", "开始日期", "结束日期", "区块天数"}
	if u.config.Mode == "daily" {
		headers = []string{"卡片 ID", "目标日期偏移"}
	}
	u.table = widget.NewTable(func() (int, int) { return len(u.model.Tasks) + 1, len(headers) }, func() fyne.CanvasObject {
		cell := &taskCell{label: widget.NewLabel(""), background: canvas.NewRectangle(color.Transparent)}
		cell.ExtendBaseWidget(cell)
		return cell
	}, func(id widget.TableCellID, object fyne.CanvasObject) {
		cell := object.(*taskCell)
		label := cell.label
		cell.background.FillColor = color.Transparent
		if id.Row > 0 && id.Row-1 == u.selected {
			cell.background.FillColor = theme.SelectionColor()
		}
		cell.background.Refresh()
		label.TextStyle = fyne.TextStyle{Bold: id.Row == 0}
		if id.Row == 0 {
			label.SetText(headers[id.Col])
			return
		}
		if id.Row > len(u.model.Tasks) {
			label.SetText("")
			return
		}
		t := u.model.Tasks[id.Row-1]
		values := []string{strconv.Itoa(t.Card), t.Start, t.End, strconv.Itoa(t.ChunkDays)}
		if u.config.Mode == "daily" {
			offset := fmt.Sprintf("%d 天前", t.Offset)
			if t.Offset == 0 {
				offset = "当天"
			}
			values = []string{strconv.Itoa(t.Card), offset}
		}
		label.SetText(values[id.Col])
	})
	u.table.HideSeparators = true
	for index := range headers {
		width := float32(130)
		if u.config.Mode == "daily" {
			width = 250
		}
		u.table.SetColumnWidth(index, width)
	}
	u.table.OnSelected = func(id widget.TableCellID) {
		if id.Row > 0 && id.Row <= len(u.model.Tasks) {
			u.selected = id.Row - 1
			u.loadTask(u.model.Tasks[u.selected])
			u.syncTaskSelection()
		} else {
			u.selected = -1
			u.syncTaskSelection()
		}
		u.table.Refresh()
	}
}

func (u *launcherUI) currentTask() (task, error) {
	t := task{}
	var err error
	t.Card, err = strconv.Atoi(strings.TrimSpace(u.entries["card"].Text))
	if err != nil {
		return t, fmt.Errorf("任务卡片 ID 必须填写整数")
	}
	if u.config.Mode == "daily" {
		t.Offset, err = strconv.Atoi(strings.TrimSpace(u.entries["offset"].Text))
	} else {
		t.Start, t.End = strings.TrimSpace(u.entries["start"].Text), strings.TrimSpace(u.entries["end"].Text)
		if u.latest.Checked {
			t.End = yesterday()
		}
		t.ChunkDays, err = strconv.Atoi(strings.TrimSpace(u.entries["chunk_days"].Text))
	}
	if err != nil {
		return t, fmt.Errorf("任务偏移或区块天数必须填写整数")
	}
	return t, validateTask(u.config.Mode, t)
}

func (u *launcherUI) editTask(update bool) {
	t, err := u.currentTask()
	if err != nil {
		u.showError(err)
		return
	}
	if update && (u.selected < 0 || u.selected >= len(u.model.Tasks)) {
		u.showError(fmt.Errorf("请先选择需要更新的任务"))
		return
	}
	for index, existing := range u.model.Tasks {
		if update && index == u.selected {
			continue
		}
		if (u.config.Mode == "daily" && existing.Card == t.Card) || (u.config.Mode == "backfill" && existing == t) {
			u.showError(fmt.Errorf("该任务已存在"))
			return
		}
	}
	if update {
		u.model.Tasks[u.selected] = t
	} else {
		u.model.Tasks = append(u.model.Tasks, t)
	}
	u.refreshTasks()
	u.status.SetText("任务清单已更新，点击“保存配置”后写入客户目录。")
}

func (u *launcherUI) loadTask(t task) {
	u.entries["card"].SetText(strconv.Itoa(t.Card))
	if u.config.Mode == "daily" {
		u.entries["offset"].SetText(strconv.Itoa(t.Offset))
	} else {
		u.entries["start"].SetText(t.Start)
		u.entries["end"].SetText(t.End)
		u.latest.SetChecked(t.End == yesterday())
		u.entries["chunk_days"].SetText(strconv.Itoa(t.ChunkDays))
	}
}

func (u *launcherUI) refreshTasks() {
	u.selected = -1
	u.table.UnselectAll()
	u.table.Refresh()
	u.count.SetText(fmt.Sprintf("%d 条任务", len(u.model.Tasks)))
	if len(u.model.Tasks) == 0 {
		u.table.Hide()
		u.emptyTasks.Show()
	} else {
		u.table.Show()
		u.emptyTasks.Hide()
	}
	if u.removedTask == nil {
		u.undoDelete.Hide()
	} else {
		u.undoDelete.Show()
	}
	u.syncTaskSelection()
}

func (u *launcherUI) restoreTask() {
	if u.removedTask == nil {
		return
	}
	for _, existing := range u.model.Tasks {
		if (u.config.Mode == "daily" && existing.Card == u.removedTask.Card) || (u.config.Mode == "backfill" && existing == *u.removedTask) {
			u.showError(fmt.Errorf("该任务已存在，无需重复恢复"))
			return
		}
	}
	index := min(u.removedIndex, len(u.model.Tasks))
	u.model.Tasks = append(u.model.Tasks, task{})
	copy(u.model.Tasks[index+1:], u.model.Tasks[index:])
	u.model.Tasks[index] = *u.removedTask
	u.removedTask = nil
	u.refreshTasks()
	u.status.SetText("已撤销删除，点击“保存配置”后写入客户目录。")
}

func (u *launcherUI) syncTaskSelection() {
	selected := u.selected >= 0 && u.selected < len(u.model.Tasks)
	for _, button := range u.taskButtons {
		if selected {
			button.Enable()
		} else {
			button.Disable()
		}
	}
	if selected {
		u.editorState.SetText(fmt.Sprintf("正在编辑任务 %d", u.model.Tasks[u.selected].Card))
	} else {
		u.editorState.SetText("新任务")
	}
}
