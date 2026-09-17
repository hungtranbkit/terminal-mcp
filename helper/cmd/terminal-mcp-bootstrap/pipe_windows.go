//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"time"

	"github.com/Microsoft/go-winio"
)

// The pipe ACL: LocalSystem and Administrators only.
//
//	D:  discretionary ACL
//	(A;;GA;;;SY)  allow, generic all, LocalSystem
//	(A;;GA;;;BA)  allow, generic all, Builtin Administrators
//
// Deliberately NOT Everyone/Interactive: an unprivileged user process
// must not be able to drive a LocalSystem service. The protocol handler
// runs as the logged-in user, so it prompts for elevation rather than
// being granted a back door here.
const pipeSDDL = "D:(A;;GA;;;SY)(A;;GA;;;BA)"

// serveePipe keeps the old signature for the non-service paths; the
// service now uses servePipeUntil so SCM can actually stop it.
func serveePipe(config Config) int { return servePipeUntil(config, nil) }

// servePipeUntil is the same loop, with one addition: closing `stop` closes
// the listener, which unblocks Accept and ends the loop. Without it a
// service that reported Running had no way to reach Stopped, and Windows
// shutdown would have to kill it.
func servePipeUntil(config Config, stop <-chan struct{}) int {
	listener, err := winio.ListenPipe(PipeName, &winio.PipeConfig{
		SecurityDescriptor: pipeSDDL,
		MessageMode:        true,
		InputBufferSize:    16 * 1024,
		OutputBufferSize:   16 * 1024,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "service: cannot listen on %s: %v\n", PipeName, err)
		return 1
	}
	defer listener.Close()
	fmt.Printf("service: listening on %s\n", PipeName)

	if stop != nil {
		go func() {
			<-stop
			// Closing the listener is what unblocks Accept below; there is
			// no other way to interrupt it.
			_ = listener.Close()
		}()
	}

	for {
		connection, err := listener.Accept()
		if err != nil {
			if err == winio.ErrPipeListenerClosed {
				return 0
			}
			continue
		}
		go func(conn net.Conn) {
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(30 * time.Minute))
			raw, err := io.ReadAll(io.LimitReader(conn, 16*1024))
			if err != nil {
				return
			}
			response := handlePipeRequest(raw, config)
			payload, _ := json.Marshal(response)
			_, _ = conn.Write(payload)
		}(connection)
	}
}

func sendToPipe(request pipeRequest) (pipeResponse, error) {
	var response pipeResponse
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	connection, err := winio.DialPipeContext(ctx, PipeName)
	if err != nil {
		return response, err
	}
	defer connection.Close()
	payload, err := json.Marshal(request)
	if err != nil {
		return response, err
	}
	if _, err := connection.Write(payload); err != nil {
		return response, err
	}
	// Onboarding is long; the service answers when it is done.
	_ = connection.SetDeadline(time.Now().Add(60 * time.Minute))
	raw, err := io.ReadAll(io.LimitReader(connection, 64*1024))
	if err != nil {
		return response, err
	}
	return response, json.Unmarshal(raw, &response)
}
