package main

import (
	"errors"
	"fmt"
	"log"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

func engineIsRunning(path string) (bool, error) {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return false, err
	}
	defer windows.CloseHandle(snapshot)
	entry := windows.ProcessEntry32{Size: uint32(unsafe.Sizeof(windows.ProcessEntry32{}))}
	err = windows.Process32First(snapshot, &entry)
	for err == nil {
		if strings.EqualFold(windows.UTF16ToString(entry.ExeFile[:]), filepath.Base(path)) {
			handle, openErr := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, entry.ProcessID)
			if openErr != nil {
				return false, fmt.Errorf("无法核对同名引擎进程 %d 的路径：%w", entry.ProcessID, openErr)
			}
			buffer, size := make([]uint16, 32768), uint32(32768)
			queryErr := windows.QueryFullProcessImageName(handle, 0, &buffer[0], &size)
			windows.CloseHandle(handle)
			if queryErr != nil {
				return false, queryErr
			}
			if strings.EqualFold(filepath.Clean(windows.UTF16ToString(buffer[:size])), filepath.Clean(path)) {
				return true, nil
			}
		}
		err = windows.Process32Next(snapshot, &entry)
	}
	if !errors.Is(err, windows.ERROR_NO_MORE_FILES) {
		return false, err
	}
	return false, nil
}

func startEngine(path, dir string) error {
	command := exec.Command(path)
	command.Dir = dir
	command.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x00000010}
	if err := command.Start(); err != nil {
		return err
	}
	go func() {
		if err := command.Wait(); err != nil {
			log.Printf("引擎退出：%v", err)
		}
	}()
	return nil
}
