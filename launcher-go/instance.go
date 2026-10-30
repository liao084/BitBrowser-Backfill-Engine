package main

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
)

func (c *launcherConfig) customerDir(name string, dirs map[string]string) (string, error) {
	if err := validateCustomer(name); err != nil {
		return "", err
	}
	dir := dirs[name]
	if dir == "" {
		dir = filepath.Join(c.Root, name)
	}
	// 不允许已有符号链接将保存操作引到客户根目录外。
	if realDir, err := filepath.EvalSymlinks(dir); err == nil {
		realRoot, err := filepath.EvalSymlinks(c.Root)
		if err != nil {
			return "", err
		}
		rel, err := filepath.Rel(realRoot, realDir)
		if err != nil || rel == ".." || filepath.IsAbs(rel) || (len(rel) > 3 && rel[:3] == ".."+string(os.PathSeparator)) {
			return "", fmt.Errorf("客户目录不能位于部署根目录外")
		}
	}
	return dir, nil
}

func writeAtomically(path string, content []byte, mode os.FileMode) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".launcher-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(content); err != nil {
		file.Close()
		return err
	}
	if err = file.Chmod(mode); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

func copyEngine(source, target string) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	info, err := input.Stat()
	if err != nil {
		return err
	}
	output, err := os.CreateTemp(filepath.Dir(target), ".launcher-engine-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(output.Name())
	if _, err = io.Copy(output, input); err != nil {
		output.Close()
		return err
	}
	if err = output.Chmod(info.Mode().Perm()); err != nil {
		output.Close()
		return err
	}
	if err = output.Close(); err != nil {
		return err
	}
	return os.Rename(output.Name(), target)
}

func (c *launcherConfig) saveInstance(s settings, dirs map[string]string, start bool) (string, error) {
	if start && runtime.GOOS != "windows" {
		return "", fmt.Errorf("Mac 版用于配置与界面审计，不运行 Windows 采集 EXE；请使用“保存配置”")
	}
	content, err := c.buildEnv(s)
	if err != nil {
		return "", err
	}
	dir, err := c.customerDir(s.Customer, dirs)
	if err != nil {
		return "", err
	}
	target := filepath.Join(dir, c.EngineFilename)
	running, err := engineIsRunning(target)
	if err != nil {
		return "", err
	}
	if start && running {
		return "", fmt.Errorf("该客户引擎已经在运行，不重复启动")
	}
	if err := os.MkdirAll(dir, 0755); err != nil {
		return "", err
	}
	if err := writeAtomically(filepath.Join(dir, ".env"), []byte(content), 0600); err != nil {
		return "", fmt.Errorf("保存配置失败：%w", err)
	}
	detail := "配置已保存；未找到 _release 引擎，未执行复制。"
	source := filepath.Join(c.Root, "_release", c.EngineFilename)
	if running {
		detail = "配置已保存；实例正在运行，未覆盖其 EXE。"
	} else if info, err := os.Stat(source); err == nil && info.Mode().IsRegular() {
		if err := copyEngine(source, target); err != nil {
			return "", fmt.Errorf("配置已保存，但引擎复制失败：%w", err)
		}
		detail = "配置已保存，并已同步 _release 中的最新引擎。"
	} else if err != nil && !os.IsNotExist(err) {
		return "", fmt.Errorf("配置已保存，但无法读取 _release 引擎：%w", err)
	}
	if start {
		if err := startEngine(target, dir); err != nil {
			return "", fmt.Errorf("配置已保存，但启动失败：%w", err)
		}
		detail = "配置已保存，客户实例已启动。"
	}
	return detail + "\n" + filepath.Join(dir, ".env"), nil
}
