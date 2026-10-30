package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/app"
)

const defaultMode = "daily"

func main() {
	mode := flag.String("mode", defaultMode, "backfill 或 daily")
	root := flag.String("root", "", "客户部署根目录（可选，不是源码目录）")
	flag.Parse()
	application := app.NewWithID("com.zerog.launcher")
	application.Settings().SetTheme(newLauncherTheme())
	if err := runLauncher(application, *mode, *root); err != nil {
		fmt.Fprintln(os.Stderr, err)
	}
}

func runLauncher(application fyne.App, mode, root string) error {
	shell, err := newLauncherShell(application, mode, root)
	if err != nil {
		return err
	}
	shell.window.ShowAndRun()
	return nil
}

// -root 仍指初始模式的部署目录；另一模式使用它旁边的同级目录。
func deploymentRoots(mode, root string) (map[string]string, error) {
	if mode != "backfill" && mode != "daily" {
		return nil, fmt.Errorf("未知模式 %q，只支持 backfill、daily", mode)
	}
	if root == "" {
		executable, err := os.Executable()
		if err != nil {
			return nil, err
		}
		dir := filepath.Dir(executable)
		if filepath.Base(dir) == "MacOS" && filepath.Base(filepath.Dir(dir)) == "Contents" {
			// Mac app 的部署文件与 .app 并排，不放进 Contents 内部。
			dir = filepath.Dir(filepath.Dir(filepath.Dir(dir)))
		}
		root = filepath.Join(dir, deploymentFolder(mode))
	}
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	roots := map[string]string{}
	for _, name := range []string{"daily", "backfill"} {
		roots[name] = filepath.Join(filepath.Dir(root), deploymentFolder(name))
	}
	roots[mode] = root
	return roots, nil
}

func deploymentFolder(mode string) string {
	if mode == "daily" {
		return "dailyfill"
	}
	return "backfill"
}
