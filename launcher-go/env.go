package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
)

func envQuote(value string) string {
	return "'" + strings.NewReplacer(`\`, `\\`, `'`, `\'`).Replace(value) + "'"
}

// 解析项目模板中的 key=value、单/双引号和多行 JSON，不展开机器环境变量。
func parseEnv(content string) (map[string]string, error) {
	lines := strings.Split(strings.ReplaceAll(strings.TrimPrefix(content, "\ufeff"), "\r\n", "\n"), "\n")
	values := map[string]string{}
	for i := 0; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		key, value, ok := strings.Cut(line, "=")
		if !ok { // python-dotenv 允许不带赋值的变量声明。
			values[line] = ""
			continue
		}
		key, value = strings.TrimSpace(key), strings.TrimSpace(value)
		if key == "" {
			return nil, fmt.Errorf(".env 第 %d 行缺少变量名", i+1)
		}
		if value == "" || (value[0] != '\'' && value[0] != '"') {
			if index := strings.Index(value, " #"); index >= 0 {
				value = strings.TrimSpace(value[:index])
			}
			values[key] = value
			continue
		}
		quote := value[0]
		var result strings.Builder
		closed := false
		for pos := 1; ; pos++ {
			if pos >= len(value) {
				if i+1 >= len(lines) {
					return nil, fmt.Errorf(".env 的 %s 引号未闭合", key)
				}
				i++
				value += "\n" + lines[i]
			}
			ch := value[pos]
			if ch == quote {
				tail := strings.TrimSpace(value[pos+1:])
				if tail != "" && !strings.HasPrefix(tail, "#") {
					return nil, fmt.Errorf(".env 的 %s 引号后有非注释内容", key)
				}
				closed = true
				break
			}
			if ch == '\\' && pos+1 < len(value) {
				next := value[pos+1]
				if next == '\\' || next == quote {
					result.WriteByte(next)
					pos++
					continue
				}
				if quote == '"' {
					escapes := map[byte]byte{'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v', 'a': '\a', '\'': '\''}
					if escaped, exists := escapes[next]; exists {
						result.WriteByte(escaped)
						pos++
						continue
					}
				}
			}
			result.WriteByte(ch)
		}
		if closed {
			values[key] = result.String()
		}
	}
	return values, nil
}

// 使用结构体声明导出字段顺序，不依赖 map 的键排序。
type backfillEnvTask struct {
	Card      int    `json:"card"`
	Start     string `json:"start"`
	End       string `json:"end"`
	ChunkDays int    `json:"chunk_days"`
}

type dailyEnvTask struct {
	CardID     int `json:"card_id"`
	OffsetDays int `json:"target_date_offset_days"`
}

func (c *launcherConfig) buildEnv(s settings) (string, error) {
	if err := validateSettings(c.Mode, s); err != nil {
		return "", err
	}
	markers, platforms := c.selectedPlatforms(s)
	backfillTasks := make([]backfillEnvTask, 0, len(s.Tasks))
	dailyTasks := make([]dailyEnvTask, 0, len(s.Tasks))
	for _, t := range s.Tasks {
		if c.Mode == "daily" {
			dailyTasks = append(dailyTasks, dailyEnvTask{CardID: t.Card, OffsetDays: t.Offset})
		} else {
			backfillTasks = append(backfillTasks, backfillEnvTask{Card: t.Card, Start: t.Start, End: t.End, ChunkDays: t.ChunkDays})
		}
	}
	var result strings.Builder
	result.WriteString("# 由 Go Launcher 生成基础配置；可在保存后手动补充。\n# 注意：再次通过 Launcher 保存会覆盖手动修改。\n\n")
	write := func(key, value string) { fmt.Fprintf(&result, "%s=%s\n", key, envQuote(value)) }
	jsonWrite := func(key string, value any) error {
		content, err := json.MarshalIndent(value, "", "    ")
		if err == nil {
			write(key, string(content))
		}
		return err
	}
	if c.Mode == "backfill" {
		write("BROWSER_TYPE", s.BrowserType)
		write("CDP_ADDRESS", s.CDPAddress)
	}
	write("BITE_ID", s.BiteID)
	write("CUSTOMER_NAME", s.Customer)
	if err := jsonWrite("GC_PAGE_URL_MARKERS", markers); err != nil {
		return "", err
	}
	write("WORKER_HEARTBEAT_SILENCE_SECONDS", strconv.Itoa(s.WorkerSilence))
	write("BUSINESS_HEARTBEAT_SILENCE_SECONDS", strconv.Itoa(s.BusinessSilence))
	write("MAX_ATTEMPTS", strconv.Itoa(s.MaxAttempts))
	if c.Mode == "backfill" {
		write("BACKFILL_CDP_SESSION_LIFETIME_HOURS", strconv.FormatFloat(s.SessionHours, 'f', -1, 64))
		write("BACKFILL_MAX_CDP_REBUILDS", strconv.Itoa(s.MaxRebuilds))
		if err := jsonWrite("TASKS_CONFIG", backfillTasks); err != nil {
			return "", err
		}
	} else {
		write("REPORT_READY_TIME", s.ReportTime)
		write("WORKER_COUNT", strconv.Itoa(s.WorkerCount))
		write("KEEP_BROWSER_AFTER_RUN", strconv.FormatBool(s.KeepBrowser))
		write("TARGET_DATE_OFFSET_DAYS", strconv.Itoa(s.DefaultOffset))
		write("TARGET_DATE", "")
		write("TIME_TYPE", s.TimeType)
		write("COOKIE_DIR", s.CookieDir)
		write("TASK_URL", s.TaskURL)
		if err := jsonWrite("DAILY_TASKS", dailyTasks); err != nil {
			return "", err
		}
		if err := jsonWrite("PLATFORMS", platforms); err != nil {
			return "", err
		}
	}
	return result.String(), nil
}

func (c *launcherConfig) selectedPlatforms(s settings) ([]string, []map[string]any) {
	markers, platforms := []string{}, []map[string]any{}
	selected := map[string]bool{}
	for _, name := range s.PlatformNames {
		selected[name] = true
	}
	addMarker := func(marker string) {
		marker = strings.TrimSpace(marker)
		for _, existing := range markers {
			if existing == marker {
				return
			}
		}
		if marker != "" {
			markers = append(markers, marker)
		}
	}
	for _, platform := range c.Platforms {
		if !selected[text(platform, "name", "")] {
			continue
		}
		for _, marker := range stringList(platform["markers"]) {
			addMarker(marker)
		}
		// 保留 auth_mode、auth_params 等未知平台字段，不缩减为固定结构。
		copy := map[string]any{}
		for key, value := range platform {
			if key != "markers" {
				copy[key] = value
			}
		}
		platforms = append(platforms, copy)
	}
	for _, marker := range s.CustomMarkers {
		addMarker(marker)
	}
	return markers, platforms
}

func (c *launcherConfig) loadEnv(path string, s settings) (settings, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return s, err
	}
	values, err := parseEnv(string(content))
	if err != nil {
		return s, err
	}
	get := func(key, fallback string) string {
		if value := values[key]; value != "" {
			return value
		}
		return fallback
	}
	integer := func(key string, fallback int) int {
		value, parseErr := strconv.Atoi(get(key, strconv.Itoa(fallback)))
		if parseErr != nil && err == nil {
			err = fmt.Errorf("%s 必须是整数", key)
		}
		return value
	}
	s.BiteID = get("BITE_ID", "")
	s.WorkerSilence = integer("WORKER_HEARTBEAT_SILENCE_SECONDS", 120)
	s.BusinessSilence = integer("BUSINESS_HEARTBEAT_SILENCE_SECONDS", 180)
	s.MaxAttempts = integer("MAX_ATTEMPTS", 5)
	if c.Mode == "backfill" {
		s.BrowserType = get("BROWSER_TYPE", "bitbrowser")
		s.CDPAddress = get("CDP_ADDRESS", "")
		s.MaxRebuilds = integer("BACKFILL_MAX_CDP_REBUILDS", 5)
		var hoursErr error
		s.SessionHours, hoursErr = strconv.ParseFloat(get("BACKFILL_CDP_SESSION_LIFETIME_HOURS", "3.5"), 64)
		if err == nil {
			err = hoursErr
		}
	} else {
		s.WorkerCount = integer("WORKER_COUNT", 1)
		s.DefaultOffset = integer("TARGET_DATE_OFFSET_DAYS", 1)
		s.TimeType = get("TIME_TYPE", "日")
		s.CookieDir = get("COOKIE_DIR", s.CookieDir)
		keepBrowser := get("KEEP_BROWSER_AFTER_RUN", strconv.FormatBool(s.KeepBrowser))
		s.KeepBrowser = strings.EqualFold(keepBrowser, "true") || keepBrowser == "1" || strings.EqualFold(keepBrowser, "yes")
	}
	if err != nil {
		return s, err
	}
	var markers []string
	if err := json.Unmarshal([]byte(get("GC_PAGE_URL_MARKERS", "[]")), &markers); err != nil {
		return s, fmt.Errorf("GC_PAGE_URL_MARKERS：%w", err)
	}
	selectedNames := map[string]bool{}
	if c.Mode == "daily" {
		var platforms []map[string]any
		if err := decodeJSON([]byte(get("PLATFORMS", "[]")), &platforms); err != nil {
			return s, fmt.Errorf("PLATFORMS：%w", err)
		}
		for _, platform := range platforms {
			selectedNames[text(platform, "name", "")] = true
		}
	}
	s.PlatformNames, s.CustomMarkers = nil, nil
	knownMarkers := map[string]bool{}
	for _, platform := range c.Platforms {
		name, platformMarkers := text(platform, "name", ""), stringList(platform["markers"])
		selected := selectedNames[name]
		if c.Mode == "backfill" {
			selected = len(platformMarkers) > 0
			for _, marker := range platformMarkers {
				found := false
				for _, existing := range markers {
					found = found || marker == existing
				}
				selected = selected && found
			}
		}
		if selected {
			s.PlatformNames = append(s.PlatformNames, name)
			for _, marker := range platformMarkers {
				knownMarkers[marker] = true
			}
		}
	}
	for _, marker := range markers {
		if !knownMarkers[marker] {
			s.CustomMarkers = append(s.CustomMarkers, marker)
		}
	}
	key := "TASKS_CONFIG"
	if c.Mode == "daily" {
		key = "DAILY_TASKS"
	}
	var rawTasks []map[string]any
	if err := decodeJSON([]byte(get(key, "[]")), &rawTasks); err != nil {
		return s, fmt.Errorf("%s：%w", key, err)
	}
	s.Tasks = nil
	for _, raw := range rawTasks {
		t := task{Card: number(raw, "card", 0), Start: text(raw, "start", ""), End: text(raw, "end", ""), ChunkDays: number(raw, "chunk_days", 1)}
		if c.Mode == "daily" {
			t = task{Card: number(raw, "card_id", 0), Offset: number(raw, "target_date_offset_days", s.DefaultOffset)}
		}
		if err := validateTask(c.Mode, t); err != nil {
			return s, err
		}
		s.Tasks = append(s.Tasks, t)
	}
	return s, nil
}
