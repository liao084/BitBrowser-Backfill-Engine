//go:build !windows

package main

import "fmt"

func engineIsRunning(string) (bool, error) { return false, nil }

func startEngine(string, string) error {
	return fmt.Errorf("请在 Windows 版本中启动采集引擎")
}
