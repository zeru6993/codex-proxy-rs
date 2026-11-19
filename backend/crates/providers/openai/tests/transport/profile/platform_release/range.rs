//! 验证 HTTP Range 的 ETag、精确范围与取消传播

use provider_openai::transport::profile::platform_release::range::RemoteFile;
use std::io::{self, Read, Seek, SeekFrom};
use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt as _, AsyncWriteExt as _, BufReader};
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;
use wiremock::{
    Mock, MockServer, ResponseTemplate,
    matchers::{header, method},
};

#[tokio::test]
async fn ranged_reads_require_one_etag_and_the_exact_requested_range() {
    for (etag, content_range, fails) in [
        ("\"first\"", "bytes 0-7/8", false),
        ("\"second\"", "bytes 0-7/8", true),
        ("\"first\"", "bytes 1-8/9", true),
    ] {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(header("range", "bytes=0-0"))
            .respond_with(
                ResponseTemplate::new(206)
                    .insert_header("etag", "\"first\"")
                    .insert_header("content-range", "bytes 0-0/8")
                    .set_body_bytes(vec![0]),
            )
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(header("range", "bytes=0-7"))
            .and(header("if-match", "\"first\""))
            .respond_with(
                ResponseTemplate::new(206)
                    .insert_header("etag", etag)
                    .insert_header("content-range", content_range)
                    .set_body_bytes(vec![1; 8]),
            )
            .mount(&server)
            .await;
        let mut reader = RemoteFile::open(
            reqwest::Client::new(),
            server.uri(),
            CancellationToken::new(),
        )
        .await
        .unwrap();
        let result = tokio::task::spawn_blocking(move || reader.read_to_end(&mut Vec::new()))
            .await
            .unwrap();
        assert_eq!(result.is_err(), fails);
    }
}

#[tokio::test]
async fn failed_ranges_log_received_bytes_and_cause_without_changing_reader_errors() {
    for (case, reason, bytes_read) in [
        ("stall", "timeout", 2),
        ("disconnect", "connection_interrupted", 2),
        ("changed", "etag_mismatch", 0),
        ("precondition", "etag_mismatch", 0),
    ] {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/?private-marker", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            for first in [true, false] {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut headers = BufReader::new(&mut socket).lines();
                while let Some(line) = headers.next_line().await.unwrap() {
                    if line.is_empty() {
                        break;
                    }
                }
                let reply = if first {
                    "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 0-0/10\r\nContent-Length: 1\r\nETag: \"one\"\r\nConnection: close\r\n\r\nx"
                } else if case == "precondition" {
                    "HTTP/1.1 412 Precondition Failed\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                } else if case == "changed" {
                    "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 3-9/10\r\nContent-Length: 7\r\nETag: \"secret-tag\"\r\nConnection: close\r\n\r\n1234567"
                } else {
                    "HTTP/1.1 206 Partial Content\r\nContent-Range: bytes 3-9/10\r\nContent-Length: 7\r\nETag: \"one\"\r\nConnection: close\r\n\r\nab"
                };
                socket.write_all(reply.as_bytes()).await.unwrap();
                if !first && case == "stall" {
                    std::future::pending::<()>().await;
                }
            }
        });
        let client = reqwest::Client::builder()
            .no_proxy()
            .read_timeout(Duration::from_secs(2))
            .build()
            .unwrap();
        let mut reader = RemoteFile::open(client, url, CancellationToken::new())
            .await
            .unwrap();
        let log = tempfile::NamedTempFile::new().unwrap();
        let subscriber = tracing_subscriber::fmt()
            .json()
            .with_writer(std::sync::Mutex::new(log.reopen().unwrap()))
            .finish();
        let error = tokio::task::spawn_blocking(move || {
            tracing::subscriber::with_default(subscriber, || {
                reader.seek(SeekFrom::Start(3)).unwrap();
                reader.read_exact(&mut [0; 7]).unwrap_err()
            })
        })
        .await
        .unwrap();
        server.abort();
        let text = std::fs::read_to_string(log.path()).unwrap();
        let records: Vec<serde_json::Value> = text
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        let mut failures = records
            .iter()
            .map(|entry| &entry["fields"])
            .filter(|fields| fields["message"] == "Desktop artifact range failed");
        let fields = failures.next().expect("range failure must be logged");
        assert!(failures.next().is_none());
        assert_eq!(fields["range_start"], 3);
        assert_eq!(fields["range_end"], 9);
        assert_eq!(fields["reason"], reason);
        assert_eq!(fields["bytes_read"], bytes_read);
        assert!(!fields["cause"].as_str().unwrap().is_empty());
        assert!(!text.contains("private-marker") && !text.contains("secret-tag"));
        if reason == "etag_mismatch" {
            assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        } else {
            assert_eq!(error.to_string(), "官方 Desktop 制品读取失败");
        }
    }
}

#[tokio::test]
async fn cancelled_reads_should_terminate_standard_read_helpers() {
    let cancellation = CancellationToken::new();
    let (_server, mut reader) = open_reader(cancellation.clone()).await;
    cancellation.cancel();

    tokio::task::spawn_blocking(move || {
        // 先验证单次 read，回归时直接失败，避免 read_exact 的自动重试挂住测试进程
        assert_eq!(
            reader.read(&mut [0]).unwrap_err().kind(),
            io::ErrorKind::Other,
        );
        assert_eq!(
            reader.read_exact(&mut [0]).unwrap_err().kind(),
            io::ErrorKind::Other,
        );
        assert_eq!(
            reader.read_to_end(&mut Vec::new()).unwrap_err().kind(),
            io::ErrorKind::Other,
        );
    })
    .await
    .unwrap();
}

#[tokio::test]
async fn cancellation_during_range_request_should_terminate_blocking_read() {
    let cancellation = CancellationToken::new();
    let _cancel_on_drop = cancellation.clone().drop_guard();
    let (server, mut reader) = open_reader(cancellation.clone()).await;
    let requested = Arc::new(Notify::new());
    let request_started = Arc::clone(&requested);
    Mock::given(method("GET"))
        .and(header("range", "bytes=0-7"))
        .and(header("if-match", "\"first\""))
        .respond_with(move |_: &wiremock::Request| {
            request_started.notify_one();
            ResponseTemplate::new(206)
                .insert_header("etag", "\"first\"")
                .insert_header("content-range", "bytes 0-7/8")
                .set_body_bytes(vec![1; 8])
                .set_delay(Duration::from_secs(60))
        })
        .expect(1)
        .mount(&server)
        .await;
    let read = tokio::task::spawn_blocking(move || reader.read(&mut [0]));
    tokio::time::timeout(Duration::from_secs(5), requested.notified())
        .await
        .expect("range request started");
    cancellation.cancel();

    let error = tokio::time::timeout(Duration::from_secs(5), read)
        .await
        .expect("cancelled blocking read completed")
        .unwrap()
        .unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::Other);
}

async fn open_reader(cancellation: CancellationToken) -> (MockServer, RemoteFile) {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(header("range", "bytes=0-0"))
        .respond_with(
            ResponseTemplate::new(206)
                .insert_header("etag", "\"first\"")
                .insert_header("content-range", "bytes 0-0/8")
                .set_body_bytes(vec![0]),
        )
        .expect(1)
        .mount(&server)
        .await;
    let reader = RemoteFile::open(reqwest::Client::new(), server.uri(), cancellation)
        .await
        .unwrap();
    (server, reader)
}
