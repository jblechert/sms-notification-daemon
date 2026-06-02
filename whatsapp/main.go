package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

const dbPath = "/var/lib/sms-notification-daemon/whatsapp.db"

type Event struct {
	From string `json:"from"`
	Text string `json:"text"`
}

func emit(from, text string) {
	b, _ := json.Marshal(Event{From: from, Text: text})
	fmt.Println(string(b))
}

func handler(evt interface{}) {
	msg, ok := evt.(*events.Message)
	if !ok {
		return
	}
	// Ignore own messages
	if msg.Info.IsFromMe {
		return
	}

	var text string
	conv := msg.Message.GetConversation()
	ext := msg.Message.GetExtendedTextMessage()
	switch {
	case conv != "":
		text = conv
	case ext != nil:
		text = ext.GetText()
	default:
		// Try image/video caption
		if img := msg.Message.GetImageMessage(); img != nil {
			text = img.GetCaption()
		} else if vid := msg.Message.GetVideoMessage(); vid != nil {
			text = vid.GetCaption()
		}
	}
	if text == "" {
		return
	}

	// Prefer push name (contact name), fall back to number
	from := msg.Info.PushName
	if from == "" {
		from = msg.Info.Sender.User
	}
	emit(from, text)
}

func main() {
	logger := waLog.Noop

	ctx := context.Background()

	container, err := sqlstore.New(ctx, "sqlite3", "file:"+dbPath+"?_foreign_keys=on", logger)
	if err != nil {
		fmt.Fprintln(os.Stderr, "DB error:", err)
		os.Exit(1)
	}

	device, err := container.GetFirstDevice(ctx)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Device error:", err)
		os.Exit(1)
	}

	client := whatsmeow.NewClient(device, logger)
	client.AddEventHandler(handler)

	if client.Store.ID == nil {
		// First run: show QR code for pairing
		qrChan, _ := client.GetQRChannel(context.Background())
		err = client.Connect()
		if err != nil {
			fmt.Fprintln(os.Stderr, "Connect error:", err)
			os.Exit(1)
		}
		for evt := range qrChan {
			if evt.Event == "code" {
				fmt.Fprintln(os.Stderr, "QR Code (scan with WhatsApp):", evt.Code)
			} else {
				fmt.Fprintln(os.Stderr, "QR event:", evt.Event)
			}
		}
	} else {
		err = client.Connect()
		if err != nil {
			fmt.Fprintln(os.Stderr, "Connect error:", err)
			os.Exit(1)
		}
	}

	// Keep running until signal
	c := make(chan os.Signal, 1)
	signal.Notify(c, os.Interrupt, syscall.SIGTERM)
	<-c
	client.Disconnect()
}

// suppress unused import warning for proto
var _ = proto.Marshal
var _ *waE2E.Message
