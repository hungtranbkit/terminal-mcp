//go:build !windows

package main

// Non-Windows stubs. The decision logic in servicemode.go is pure and is
// tested everywhere; only the dispatch is Windows-only.

func runServiceSCM() int { return 3 }

// isWindowsService: nothing outside Windows has a Service Control Manager,
// and saying so plainly is better than an error that callers must special
// case.
func isWindowsService() (bool, error) { return false, nil }
