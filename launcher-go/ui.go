package main

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

type launcherUI struct {
	application   fyne.App
	window        fyne.Window
	config        *launcherConfig
	model         settings
	dirs          map[string]string
	loading       bool
	loadErr       error
	customerNames []string
	customer      *customerEntry
	entries       map[string]*widget.Entry
	platforms     map[string]*widget.Check
	browser       *widget.Select
	timeType      *widget.Select
	keepBrowser   *widget.Check
	latest        *widget.Check
	table         *widget.Table
	selected      int
	status        *widget.Label
	count         *widget.Label
	authority     *widget.Label
	content       fyne.CanvasObject
	taskButtons   []*widget.Button
	editorState   *widget.Label
	emptyTasks    *widget.Label
	undoDelete    *widget.Button
	removedTask   *task
	removedIndex  int
}

func newLauncherUI(application fyne.App, config *launcherConfig) (*launcherUI, error) {
	window := application.NewWindow("采集任务配置")
	u, err := newModeUI(application, window, config)
	if err != nil {
		window.Close()
		return nil, err
	}
	window.SetContent(u.content)
	window.Resize(fyne.NewSize(1200, 900))
	return u, nil
}

func newModeUI(application fyne.App, window fyne.Window, config *launcherConfig) (*launcherUI, error) {
	names, dirs, err := config.discoverCustomers()
	if err != nil {
		return nil, err
	}
	u := &launcherUI{application: application, config: config, dirs: dirs, customerNames: names, selected: -1, entries: map[string]*widget.Entry{}, platforms: map[string]*widget.Check{}, window: window}
	u.status = wrappedLabel("请选择客户，或点击“新建客户”。")
	u.count = widget.NewLabel("0 条任务")
	u.editorState = widget.NewLabel("新任务")
	u.authority = wrappedLabel("")
	u.customer = newCustomerEntry(names)
	u.customer.SetPlaceHolder("选择或输入客户名称")
	u.customer.OnChanged = func(name string) {
		if u.loading {
			return
		}
		for _, existing := range u.customerNames {
			if existing == name {
				u.loadCustomer(name)
				return
			}
		}
	}
	u.customer.OnSubmitted = func(name string) { u.loadCustomer(strings.TrimSpace(name)) }
	u.browser = widget.NewSelect([]string{"bitbrowser", "external_cdp"}, func(string) { u.updateBrowserFields() })
	u.timeType = widget.NewSelect([]string{"日", "周", "近7天", "近30天", "月"}, nil)
	u.keepBrowser = widget.NewCheck("完成后保留浏览器", nil)
	u.latest = widget.NewCheck("截至最新完整日期（昨天）", func(checked bool) {
		if u.entries["end"] == nil {
			return
		}
		if checked {
			u.entries["end"].SetText(yesterday())
			u.entries["end"].Disable()
		} else {
			u.entries["end"].Enable()
		}
		if action, ok := u.entries["end"].ActionItem.(fyne.Disableable); ok {
			if checked {
				action.Disable()
			} else {
				action.Enable()
			}
		}
	})

	sources := widget.NewAccordion(widget.NewAccordionItem("配置来源与目录", container.NewVBox(helperText("部署目录："+config.Root), helperText("主配置："+config.Path))))
	left := container.NewVBox(u.customerCard(), widget.NewSeparator(), u.runtimeCard(), widget.NewSeparator(), u.platformCard(), widget.NewSeparator(), sources)
	u.buildTaskTable()
	listTitle := container.NewBorder(nil, nil, heading("任务清单", theme.SizeNameSubHeadingText), u.count)
	columns := 4
	if config.Mode == "daily" {
		columns = 2
	}
	table := container.New(&taskTableLayout{table: u.table, columns: columns}, u.table)
	u.emptyTasks = wrappedLabel("暂无任务，在上方填写后点击“新增任务”。")
	u.emptyTasks.Alignment = fyne.TextAlignCenter
	u.undoDelete = widget.NewButton("撤销删除", u.restoreTask)
	u.undoDelete.Importance = widget.LowImportance
	right := container.NewBorder(container.NewVBox(u.taskEditor(), inset(widget.NewSeparator(), 12, 0), listTitle), container.NewBorder(nil, nil, nil, u.undoDelete, helperText("选择表格行以编辑，列表更改需另行保存。")), nil, nil, container.NewStack(table, container.NewCenter(u.emptyTasks)))
	split := container.NewHSplit(panel(container.NewVScroll(inset(left, 12, 20)), theme.ColorNameBackground), panel(inset(right, 20, 24), theme.ColorNameInputBackground))
	split.Offset = 0.40
	preview := widget.NewButtonWithIcon("预览 .env", theme.DocumentIcon(), func() {
		s, err := u.readSettings()
		if err != nil {
			u.showError(err)
			return
		}
		content, err := u.config.buildEnv(s)
		if err != nil {
			u.showError(err)
			return
		}
		entry := widget.NewMultiLineEntry()
		entry.SetText(content)
		entry.Disable()
		popup := dialog.NewCustom("配置预览（只读，尚未保存）", "关闭", entry, u.window)
		popup.Resize(fyne.NewSize(780, 600))
		popup.Show()
	})
	save := widget.NewButtonWithIcon("保存配置", theme.DocumentSaveIcon(), func() { u.save(false) })
	start := widget.NewButtonWithIcon("保存并启动", theme.MediaPlayIcon(), func() { u.save(true) })
	start.Importance = widget.HighImportance
	if runtime.GOOS != "windows" {
		start.Disable()
		save.Importance = widget.HighImportance
	}
	actions := container.NewGridWithColumns(3, preview, save, start)
	footer := inset(container.NewVBox(container.NewBorder(nil, nil, nil, actions, u.status), helperText("保存会覆盖手工补充项，请先备份。")), 12, 24)
	u.content = container.NewBorder(nil, container.NewVBox(widget.NewSeparator(), footer), nil, nil, split)
	name := ""
	if len(names) > 0 {
		name = names[0]
	}
	u.loadCustomer(name)
	return u, nil
}

func wrappedLabel(value string) *widget.Label {
	label := widget.NewLabel(value)
	label.Wrapping = fyne.TextWrapWord
	return label
}

func (u *launcherUI) entry(key, placeholder string) *widget.Entry {
	if existing := u.entries[key]; existing != nil {
		return existing
	}
	entry := widget.NewEntry()
	entry.SetPlaceHolder(placeholder)
	if limits, ok := numericRange(u.config.Mode, key); ok {
		newNumberArrows(entry, limits)
	}
	u.entries[key] = entry
	return entry
}

func (u *launcherUI) customerCard() fyne.CanvasObject {
	form := container.NewVBox(field("BITE_ID", u.entry("bite_id", "BitBrowser 窗口 ID")))
	if u.config.Mode == "backfill" {
		form.Add(field("浏览器来源", u.browser))
		form.Add(field("CDP 地址", u.entry("cdp_address", "127.0.0.1:9222")))
	}
	newButton := widget.NewButton("新建客户", func() { u.loadCustomer(""); u.window.Canvas().Focus(u.customer) })
	loadButton := widget.NewButton("加载 .env", func() { u.loadCustomer(strings.TrimSpace(u.customer.Text)) })
	folderButton := widget.NewButton("客户目录", u.openCustomerFolder)
	return section("客户与浏览器", container.NewVBox(field("客户", u.customer), container.NewGridWithColumns(3, newButton, loadButton, folderButton), form))
}

func (u *launcherUI) runtimeCard() fyne.CanvasObject {
	form := container.New(fieldGridLayout{}, field("Worker 静默 / 秒", u.entry("worker_silence", "120")), field("业务页静默 / 秒", u.entry("business_silence", "180")), field("最多尝试次数", u.entry("max_attempts", "5")))
	if u.config.Mode == "backfill" {
		form.Add(field("会话时长 / 小时", u.entry("session_hours", "3.5")))
		form.Add(field("最多 CDP 重建", u.entry("max_rebuilds", "5")))
	} else {
		form.Add(field("Worker 数量", u.entry("worker_count", "1")))
		form.Add(field("全局日期偏移 / 天", u.entry("default_offset", "1")))
		form.Add(field("时间维度", u.timeType))
	}
	content := container.NewVBox(form)
	if u.config.Mode == "daily" {
		content.Add(u.keepBrowser)
		content.Add(widget.NewAccordion(widget.NewAccordionItem("报告时间 / Cookie / 任务入口", u.authority)))
	}
	content.Add(helperText("业务页静默应大于 Worker 静默。"))
	return section("运行参数", content)
}

func (u *launcherUI) platformCard() fyne.CanvasObject {
	checks := container.NewGridWithColumns(2)
	for _, platform := range u.config.Platforms {
		name := text(platform, "name", "")
		check := widget.NewCheck(name, nil)
		u.platforms[name] = check
		checks.Add(check)
	}
	return section("业务平台", container.NewVBox(checks, field("其他 URL 标识", u.entry("markers", "多个标识以英文逗号分隔"))))
}

func (u *launcherUI) updateBrowserFields() {
	if u.entries["bite_id"] == nil || u.entries["cdp_address"] == nil {
		return
	}
	u.entries["bite_id"].Enable()
	u.entries["cdp_address"].Enable()
	if u.browser.Selected == "external_cdp" {
		u.entries["bite_id"].Disable()
	} else {
		u.entries["cdp_address"].Disable()
	}
}

func (u *launcherUI) loadCustomer(name string) {
	u.loading = true
	defer func() { u.loading = false }()
	u.model, u.loadErr = u.config.initialSettings(name), nil
	u.removedTask = nil
	u.selected = -1
	u.customer.SetText(name)
	status := "新建客户：填写名称、浏览器参数，并添加任务。"
	if name != "" {
		dir, err := u.config.customerDir(name, u.dirs)
		if err != nil {
			u.loadErr = err
		} else if _, err := os.Stat(filepath.Join(dir, ".env")); err == nil {
			loaded, err := u.config.loadEnv(filepath.Join(dir, ".env"), u.model)
			if err != nil {
				u.loadErr = err
			} else {
				u.model = loaded
				status = "已加载：" + filepath.Join(dir, ".env")
			}
		} else if !os.IsNotExist(err) {
			u.loadErr = err
		} else {
			status = "该客户暂无 .env，已应用 Launcher 默认配置。"
		}
	}
	u.setFields()
	if u.loadErr != nil {
		u.showError(fmt.Errorf("客户配置加载失败，已禁止保存以免覆盖原文件：%w", u.loadErr))
	} else {
		u.status.SetText(status)
	}
}

func (u *launcherUI) setFields() {
	s := u.model
	values := map[string]string{"bite_id": s.BiteID, "cdp_address": s.CDPAddress, "worker_silence": strconv.Itoa(s.WorkerSilence), "business_silence": strconv.Itoa(s.BusinessSilence), "max_attempts": strconv.Itoa(s.MaxAttempts), "max_rebuilds": strconv.Itoa(s.MaxRebuilds), "session_hours": strconv.FormatFloat(s.SessionHours, 'f', -1, 64), "worker_count": strconv.Itoa(s.WorkerCount), "default_offset": strconv.Itoa(s.DefaultOffset), "markers": strings.Join(s.CustomMarkers, ", "), "card": "1", "chunk_days": "1", "offset": strconv.Itoa(s.DefaultOffset), "start": yesterday(), "end": yesterday()}
	for key, value := range values {
		if entry := u.entries[key]; entry != nil {
			entry.SetText(value)
		}
	}
	u.browser.SetSelected(s.BrowserType)
	u.timeType.SetSelected(s.TimeType)
	u.keepBrowser.SetChecked(s.KeepBrowser)
	u.latest.SetChecked(true)
	for name, check := range u.platforms {
		selected := false
		for _, platformName := range s.PlatformNames {
			selected = selected || name == platformName
		}
		check.SetChecked(selected)
	}
	u.authority.SetText("报告准备时间：" + s.ReportTime + "\nCookie 目录：" + s.CookieDir + "\n任务入口：" + s.TaskURL)
	u.updateBrowserFields()
	u.refreshTasks()
}

func (u *launcherUI) readSettings() (settings, error) {
	if u.loadErr != nil {
		return settings{}, fmt.Errorf("原配置未成功加载，不能保存；请修复后重新加载，或明确新建其他客户：%w", u.loadErr)
	}
	s := u.model
	s.Customer = strings.TrimSpace(u.customer.Text)
	s.BiteID = strings.TrimSpace(u.entries["bite_id"].Text)
	s.CustomMarkers = strings.Split(u.entries["markers"].Text, ",")
	s.PlatformNames = nil
	for _, platform := range u.config.Platforms {
		name := text(platform, "name", "")
		if u.platforms[name].Checked {
			s.PlatformNames = append(s.PlatformNames, name)
		}
	}
	for key, target := range map[string]*int{"worker_silence": &s.WorkerSilence, "business_silence": &s.BusinessSilence, "max_attempts": &s.MaxAttempts, "max_rebuilds": &s.MaxRebuilds, "worker_count": &s.WorkerCount, "default_offset": &s.DefaultOffset} {
		if entry := u.entries[key]; entry != nil {
			value, err := strconv.Atoi(strings.TrimSpace(entry.Text))
			if err != nil {
				return s, fmt.Errorf("%s 必须填写整数", key)
			}
			*target = value
		}
	}
	if u.config.Mode == "backfill" {
		s.BrowserType = u.browser.Selected
		s.CDPAddress = strings.TrimSpace(u.entries["cdp_address"].Text)
		hours, err := strconv.ParseFloat(strings.TrimSpace(u.entries["session_hours"].Text), 64)
		if err != nil {
			return s, fmt.Errorf("会话时长必须填写数字")
		}
		s.SessionHours = hours
	} else {
		s.TimeType, s.KeepBrowser = u.timeType.Selected, u.keepBrowser.Checked
	}
	return s, validateSettings(u.config.Mode, s)
}

func (u *launcherUI) save(start bool) {
	s, err := u.readSettings()
	if err != nil {
		u.showError(err)
		return
	}
	dialog.ShowConfirm("确认保存基础配置", "再次保存会覆盖客户 .env 中手工补充的字段。确认已备份并继续？", func(ok bool) {
		if !ok {
			return
		}
		detail, err := u.config.saveInstance(s, u.dirs, start)
		if err != nil {
			u.showError(err)
			return
		}
		u.status.SetText(detail)
		names, dirs, err := u.config.discoverCustomers()
		if err != nil {
			u.showError(err)
			return
		}
		u.dirs = dirs
		u.customerNames = names
		u.loading = true
		u.customer.SetOptions(names)
		u.loading = false
		if start {
			u.window.Close()
		} else {
			dialog.ShowInformation("保存成功", detail, u.window)
		}
	}, u.window)
}

func (u *launcherUI) openCustomerFolder() {
	dir, err := u.config.customerDir(strings.TrimSpace(u.customer.Text), u.dirs)
	if err == nil {
		err = os.MkdirAll(dir, 0755)
	}
	if err == nil {
		err = u.application.OpenURL(&url.URL{Scheme: "file", Path: filepath.ToSlash(dir)})
	}
	if err != nil {
		u.showError(err)
	}
}

func (u *launcherUI) showError(err error) {
	u.status.SetText(err.Error())
	dialog.ShowError(err, u.window)
}

func yesterday() string { return time.Now().AddDate(0, 0, -1).Format("2006-01-02") }
