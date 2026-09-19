package messaging

import (
	"context"
	"testing"

	"github.com/fastclaw-ai/weclaw/ilink"
)

func TestExtractVoice(t *testing.T) {
	msg := ilink.WeixinMessage{ItemList: []ilink.MessageItem{
		{Type: ilink.ItemTypeText, TextItem: &ilink.TextItem{Text: "hi"}},
		{Type: ilink.ItemTypeVoice, VoiceItem: &ilink.VoiceItem{Text: "你好", Playtime: 3200, EncodeType: 6}},
	}}
	voice := extractVoice(msg)
	if voice == nil {
		t.Fatal("expected voice item")
	}
	if voice.Text != "你好" || voice.Playtime != 3200 || voice.EncodeType != 6 {
		t.Fatalf("unexpected voice item: %+v", voice)
	}
	if extractVoice(ilink.WeixinMessage{}) != nil {
		t.Fatal("expected nil voice for empty message")
	}
}

func TestForwardVoiceWithoutMedia(t *testing.T) {
	handler := NewHandler(nil, nil)
	handler.SetVoiceEndpoint("http://127.0.0.1:1/voice")
	ok := handler.forwardVoice(
		context.Background(),
		ilink.WeixinMessage{FromUserID: "u@im.wechat"},
		&ilink.VoiceItem{},
	)
	if ok {
		t.Fatal("expected forward to fail when voice has no media info")
	}
}
