package main

import "testing"

func TestSetupDashboardOrigin(t *testing.T) {
	for _, tt := range []struct {
		controller, dashboard, want string
		fail                        bool
	}{
		{"https://bootstrap.example", "https://dashboard.example", "https://dashboard.example", false},
		{"https://controller.example", "", "https://controller.example", false},
		{"https://bootstrap.example", "http://dashboard.example", "", true},
		{"https://bootstrap.example", "https://user:secret@dashboard.example", "", true},
	} {
		got, err := setupDashboardOrigin(tt.controller, tt.dashboard)
		if (err != nil) != tt.fail || got != tt.want {
			t.Errorf("got %q error %v", got, err)
		}
	}
}
