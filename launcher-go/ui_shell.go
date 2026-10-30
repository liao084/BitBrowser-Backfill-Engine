package main

import (
	"runtime"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/theme"
	"fyne.io/fyne/v2/widget"
)

// 每种模式缓存独立的表单与任务。切换只替换视图，不保存或丢弃编辑。
type launcherShell struct {
	application fyne.App
	window      fyne.Window
	roots       map[string]string
	views       map[string]*launcherUI
	buttons     map[string]*widget.Button
	body        *fyne.Container
	mode        string
}

func newLauncherShell(application fyne.App, mode, root string) (*launcherShell, error) {
	roots, err := deploymentRoots(mode, root)
	if err != nil {
		return nil, err
	}
	s := &launcherShell{application: application, window: application.NewWindow("采集任务配置"), roots: roots, views: map[string]*launcherUI{}, buttons: map[string]*widget.Button{}, body: container.NewStack()}
	for _, name := range []string{"daily", "backfill"} {
		name := name
		label := "每日采集"
		if name == "backfill" {
			label = "历史补采"
		}
		s.buttons[name] = widget.NewButton(label, func() {
			if err := s.selectMode(name); err != nil {
				dialog.ShowError(err, s.window)
			}
		})
	}
	caption := "客户配置与任务管理"
	if runtime.GOOS != "windows" {
		caption = "Mac 配置审计版"
	}
	header := container.NewBorder(nil, nil, container.NewVBox(heading("采集任务配置", theme.SizeNameHeadingText), helperText(caption)), container.NewGridWithColumns(2, s.buttons["daily"], s.buttons["backfill"]))
	s.window.SetContent(container.NewBorder(inset(header, 16, 24), nil, nil, nil, s.body))
	s.window.Resize(fyne.NewSize(1200, 900))
	if err := s.selectMode(mode); err != nil {
		// 初始模式缺配置时仍允许切换到另一模式，或补好文件后重新加载。
		retry := widget.NewButton("重新加载配置", func() {
			if err := s.selectMode(mode); err != nil {
				dialog.ShowError(err, s.window)
			}
		})
		s.body.Add(inset(container.NewVBox(heading("未能加载配置", theme.SizeNameSubHeadingText), wrappedLabel(err.Error()), retry), 24, 24))
	}
	return s, nil
}

func (s *launcherShell) selectMode(mode string) error {
	view := s.views[mode]
	if view == nil {
		config, err := loadConfig(mode, s.roots[mode])
		if err != nil {
			return err
		}
		view, err = newModeUI(s.application, s.window, config)
		if err != nil {
			return err
		}
		s.views[mode] = view
	}
	s.mode = mode
	s.body.Objects = []fyne.CanvasObject{view.content}
	s.body.Refresh()
	for name, button := range s.buttons {
		button.Importance = widget.LowImportance
		if name == mode {
			button.Importance = widget.MediumImportance
		}
		button.Refresh()
	}
	return nil
}
