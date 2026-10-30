package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

type launcherConfig struct {
	Mode, Root, Path, EngineFilename string
	Customers, Platforms             []map[string]any
	Defaults                         map[string]any
}

type task struct {
	Card, ChunkDays, Offset int
	Start, End              string
}

type settings struct {
	Customer, BiteID, BrowserType, CDPAddress string
	WorkerSilence, BusinessSilence            int
	MaxAttempts, MaxRebuilds, WorkerCount     int
	SessionHours                              float64
	DefaultOffset                             int
	TimeType, CookieDir, TaskURL, ReportTime  string
	KeepBrowser                               bool
	PlatformNames, CustomMarkers              []string
	Tasks                                     []task
}

func loadConfig(mode, root string) (*launcherConfig, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	filename, engine := "backfill_launcher_config.json", "backfill_engine.exe"
	if mode == "daily" {
		filename, engine = "dailyfill_launcher_config.json", "daily_engine.exe"
	}
	path := filepath.Join(root, "_release", filename)
	content, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取 Launcher 配置失败：%s\n%w\n请将模式对应的 JSON 配置放到 _release 中。", path, err)
	}
	var raw struct {
		EngineFilename string           `json:"engine_filename"`
		Customers      []map[string]any `json:"customers"`
		Platforms      []map[string]any `json:"platforms"`
		Defaults       map[string]any   `json:"defaults"`
	}
	if err := decodeJSON(content, &raw); err != nil || strings.TrimSpace(string(content)) == "null" {
		return nil, fmt.Errorf("配置必须是有效的 JSON 对象：%s (%v)", path, err)
	}
	if raw.EngineFilename != "" {
		engine = raw.EngineFilename
	}
	if filepath.Base(engine) != engine || strings.ContainsAny(engine, `/\:`) {
		return nil, fmt.Errorf("engine_filename 必须是文件名，不能包含目录")
	}
	config := &launcherConfig{Mode: mode, Root: root, Path: path, EngineFilename: engine, Customers: raw.Customers, Defaults: raw.Defaults}
	for _, platform := range raw.Platforms {
		_, markersOK := platform["markers"].([]any)
		if text(platform, "name", "") != "" && markersOK && (mode != "daily" || text(platform, "home_url", "") != "") {
			config.Platforms = append(config.Platforms, platform)
		}
	}
	return config, nil
}

func decodeJSON(content []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(content))
	decoder.UseNumber()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return fmt.Errorf("JSON 后存在多余内容")
	}
	return nil
}

func text(values map[string]any, key, fallback string) string {
	value, ok := values[key]
	if !ok || value == nil {
		return fallback
	}
	return fmt.Sprint(value)
}

func number(values map[string]any, key string, fallback int) int {
	value, err := strconv.Atoi(text(values, key, strconv.Itoa(fallback)))
	if err != nil {
		return 0
	}
	return value
}

func stringList(value any) []string {
	result := []string{}
	if items, ok := value.([]any); ok {
		for _, item := range items {
			result = append(result, fmt.Sprint(item))
		}
	}
	return result
}

func (c *launcherConfig) initialSettings(name string) settings {
	d := c.Defaults
	hours, _ := strconv.ParseFloat(text(d, "cdp_session_lifetime_hours", "3.5"), 64)
	s := settings{Customer: name, BrowserType: text(d, "browser_type", "bitbrowser"), CDPAddress: text(d, "cdp_address", "127.0.0.1:9222"), WorkerSilence: number(d, "worker_heartbeat_seconds", 120), BusinessSilence: number(d, "business_heartbeat_seconds", 180), MaxAttempts: number(d, "max_attempts", 5), MaxRebuilds: number(d, "max_cdp_rebuilds", 5), SessionHours: hours, WorkerCount: number(d, "worker_count", 1), DefaultOffset: number(d, "target_date_offset_days", 1), TimeType: text(d, "time_type", "日"), CookieDir: text(d, "cookie_dir", "C:/Users/Administrator/Desktop/COOKIE"), TaskURL: text(d, "task_url", ""), ReportTime: strings.TrimSpace(text(d, "report_ready_time", "")), KeepBrowser: text(d, "keep_browser_after_run", "false") == "true"}
	for _, customer := range c.Customers {
		if strings.TrimSpace(text(customer, "name", "")) != name {
			continue
		}
		s.BiteID = text(customer, "bite_id", "")
		s.PlatformNames = stringList(customer["platforms"])
		if c.Mode == "daily" {
			s.WorkerCount = number(customer, "worker_count", s.WorkerCount)
			s.MaxAttempts = number(customer, "max_attempts", s.MaxAttempts)
			s.DefaultOffset = number(customer, "target_date_offset_days", s.DefaultOffset)
			s.TimeType = text(customer, "time_type", s.TimeType)
			s.ReportTime = strings.TrimSpace(text(customer, "report_ready_time", s.ReportTime))
		}
		break
	}
	return s
}

func (c *launcherConfig) discoverCustomers() ([]string, map[string]string, error) {
	names, dirs := map[string]bool{}, map[string]string{}
	for _, customer := range c.Customers {
		if name := strings.TrimSpace(text(customer, "name", "")); name != "" {
			names[name] = true
		}
	}
	if c.Mode == "backfill" {
		entries, err := os.ReadDir(c.Root)
		if err != nil && !os.IsNotExist(err) {
			return nil, nil, err
		}
		for _, entry := range entries {
			if entry.IsDir() && entry.Name() != "_release" {
				names[entry.Name()] = true
			}
		}
	} else {
		err := filepath.WalkDir(c.Root, func(path string, entry fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if entry.IsDir() && entry.Name() == "_release" {
				return filepath.SkipDir
			}
			if entry.IsDir() || entry.Name() != ".env" {
				return nil
			}
			dir := filepath.Dir(path)
			name := filepath.Base(dir)
			if previous, exists := dirs[name]; exists && previous != dir {
				return fmt.Errorf("发现重名客户目录 %q：\n%s\n%s", name, previous, dir)
			}
			dirs[name], names[name] = dir, true
			return nil
		})
		if err != nil && !os.IsNotExist(err) {
			return nil, nil, err
		}
	}
	list := make([]string, 0, len(names))
	for name := range names {
		list = append(list, name)
	}
	sort.Strings(list)
	return list, dirs, nil
}

var invalidName = regexp.MustCompile(`[<>:"/\\|?*\x00-\x1f]`)
var reportTime = regexp.MustCompile(`^(?:[01]\d|2[0-3]):[0-5]\d$`)
var reservedName = regexp.MustCompile(`(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)`)

func validateCustomer(name string) error {
	if name == "" || name != strings.TrimSpace(name) || invalidName.MatchString(name) || strings.HasSuffix(name, ".") || name == "." || name == ".." || strings.EqualFold(name, "_release") || reservedName.MatchString(name) {
		return fmt.Errorf("客户名称无效：不能为空、包含 Windows 非法字符或使用保留目录名")
	}
	return nil
}

func validateTask(mode string, t task) error {
	maxCard := 999999999
	if mode == "daily" {
		maxCard = 2147483647
	}
	if t.Card < 1 || t.Card > maxCard {
		return fmt.Errorf("任务卡片 ID 应在 1～%d 之间", maxCard)
	}
	if mode == "daily" {
		if t.Offset < 0 || t.Offset > 365 {
			return fmt.Errorf("任务目标日期偏移应在 0～365 天之间")
		}
		return nil
	}
	start, startErr := time.Parse("2006-01-02", t.Start)
	end, endErr := time.Parse("2006-01-02", t.End)
	if startErr != nil || endErr != nil || start.After(end) {
		return fmt.Errorf("任务日期应为 YYYY-MM-DD，且开始日期不能晚于结束日期")
	}
	if t.ChunkDays < 1 || t.ChunkDays > 366 {
		return fmt.Errorf("区块天数应在 1～366 之间")
	}
	return nil
}

func validateSettings(mode string, s settings) error {
	if err := validateCustomer(s.Customer); err != nil {
		return err
	}
	if s.WorkerSilence < 1 || s.WorkerSilence > 86400 || s.BusinessSilence <= s.WorkerSilence || s.BusinessSilence > 86400 {
		return fmt.Errorf("心跳静默应在 1～86400 秒之间，业务页必须大于 Worker")
	}
	if len(s.Tasks) == 0 {
		return fmt.Errorf("请至少添加一条任务")
	}
	for i, t := range s.Tasks {
		if err := validateTask(mode, t); err != nil {
			return fmt.Errorf("第 %d 条任务：%w", i+1, err)
		}
		for _, previous := range s.Tasks[:i] {
			if (mode == "daily" && previous.Card == t.Card) || (mode == "backfill" && previous == t) {
				return fmt.Errorf("任务清单包含重复任务")
			}
		}
	}
	if mode == "backfill" {
		if (s.BrowserType == "bitbrowser" && s.BiteID == "") || (s.BrowserType == "external_cdp" && s.CDPAddress == "") {
			return fmt.Errorf("请填写对应浏览器的 BITE_ID 或 CDP 地址")
		}
		if s.BrowserType != "bitbrowser" && s.BrowserType != "external_cdp" {
			return fmt.Errorf("浏览器来源只支持 bitbrowser 或 external_cdp")
		}
		if s.MaxAttempts < 1 || s.MaxAttempts > 100 || s.MaxRebuilds < 0 || s.MaxRebuilds > 100 || !(s.SessionHours >= 0.1 && s.SessionHours <= 168) {
			return fmt.Errorf("尝试次数应为 1～100，CDP 重建为 0～100，会话时长为 0.1～168 小时")
		}
	} else {
		if s.BiteID == "" || s.WorkerCount < 1 || s.WorkerCount > 20 || s.MaxAttempts < 1 || s.MaxAttempts > 20 || s.DefaultOffset < 0 || s.DefaultOffset > 365 {
			return fmt.Errorf("请填写 BITE_ID；Worker/尝试次数应为 1～20，全局日期偏移为 0～365")
		}
		if s.TimeType != "日" && s.TimeType != "周" && s.TimeType != "近7天" && s.TimeType != "近30天" && s.TimeType != "月" {
			return fmt.Errorf("时间维度只支持日、周、近7天、近30天、月")
		}
		if strings.TrimSpace(s.CookieDir) == "" || strings.TrimSpace(s.TaskURL) == "" || !reportTime.MatchString(s.ReportTime) {
			return fmt.Errorf("Launcher 配置必须提供 cookie_dir、task_url，以及 HH:MM 格式的 report_ready_time")
		}
	}
	return nil
}
