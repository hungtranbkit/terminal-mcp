//go:build !windows

package main

import "errors"

// The helper only ever runs on Windows. These exist so the package builds
// and its pure-logic tests run on a Linux CI box -- they are never a
// fallback implementation, and they say so by failing loudly.
var errNotWindows = errors.New("Terminal MCP Bootstrap runs on Windows only")

func registerProtocol() error                  { return errNotWindows }
func unregisterProtocol() error                { return errNotWindows }
func installService() error                    { return errNotWindows }
func serviceExists() bool                      { return false }
func removeService() error                     { return errNotWindows }
func runStages(controller, extraArg string) error { return errNotWindows }
