//go:build !windows

package main

import "fmt"

func serveePipe(config Config) int { fmt.Println(errNotWindows); return 1 }

func sendToPipe(request pipeRequest) (pipeResponse, error) {
	return pipeResponse{}, errNotWindows
}
