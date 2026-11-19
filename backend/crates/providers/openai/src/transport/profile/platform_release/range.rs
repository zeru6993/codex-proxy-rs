//! 将同一 ETag 的有界 HTTP Range 暴露为归档库需要的 Read + Seek

use std::error::Error;
use std::io::{self, Read, Seek, SeekFrom};
use std::time::{Duration, Instant};

use futures::StreamExt as _;
use reqwest::{Client, StatusCode, header};
use serde::{Deserialize, Serialize};
use tokio_util::sync::CancellationToken;

use super::super::desktop_artifact::parse_content_range;

const CHUNK: u64 = 1024 * 1024;
const MAX_DOWNLOAD: u64 = 512 * 1024 * 1024;

pub(super) fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "官方 Desktop 制品结构或版本不完整",
    )
}

#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub struct ArtifactIdentity {
    pub size: u64,
    pub etag: String,
}

pub struct RemoteFile {
    client: Client,
    url: String,
    pub identity: ArtifactIdentity,
    runtime: tokio::runtime::Handle,
    cancel: CancellationToken,
    deadline: Instant,
    position: u64,
    buffer_start: u64,
    buffer: Vec<u8>,
    downloaded: u64,
}

impl RemoteFile {
    pub async fn open(client: Client, url: String, cancel: CancellationToken) -> io::Result<Self> {
        let (identity, _) = request(&client, &url, 0, 0, None).await?;
        Ok(Self {
            client,
            url,
            identity,
            cancel,
            runtime: tokio::runtime::Handle::current(),
            deadline: Instant::now() + Duration::from_secs(300),
            position: 0,
            buffer_start: 0,
            buffer: Vec::new(),
            downloaded: 0,
        })
    }
}

impl Read for RemoteFile {
    fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
        let cancelled = self.cancel.is_cancelled();
        if cancelled || Instant::now() >= self.deadline {
            // 此时没有在途分段，记录读取位置，避免把总预算耗尽误报为网络中断
            tracing::warn!(
                position = self.position,
                phase = "before_read",
                reason = if cancelled { "cancelled" } else { "timeout" },
                "Desktop artifact read stopped"
            );
            // 取消和预算耗尽是终态；Interrupted 会被 read_exact/read_to_end
            // 自动重试，导致 blocking 线程空转并阻止 Tokio runtime 退出
            return Err(io::Error::other("Desktop 制品读取已取消或超时"));
        }
        if output.is_empty() || self.position >= self.identity.size {
            return Ok(0);
        }
        if self.position < self.buffer_start
            || self.position >= self.buffer_start + self.buffer.len() as u64
        {
            let end = self
                .position
                .saturating_add(CHUNK - 1)
                .min(self.identity.size - 1);
            self.downloaded += end - self.position + 1;
            if self.downloaded > MAX_DOWNLOAD {
                return Err(invalid());
            }
            let (_, bytes) = self.runtime.block_on(async {
                tokio::select! {
                    () = self.cancel.cancelled() => Err(io::Error::other("Desktop 制品读取已取消")),
                    result = request(&self.client, &self.url, self.position, end, Some(&self.identity)) => result,
                }
            })?;
            self.buffer_start = self.position;
            self.buffer = bytes;
        }
        let offset = (self.position - self.buffer_start) as usize;
        let count = output.len().min(self.buffer.len() - offset);
        output[..count].copy_from_slice(&self.buffer[offset..offset + count]);
        self.position += count as u64;
        Ok(count)
    }
}

pub(super) fn seek_position(position: u64, size: u64, seek: SeekFrom) -> io::Result<u64> {
    let next = match seek {
        SeekFrom::Start(offset) => i128::from(offset),
        SeekFrom::Current(offset) => i128::from(position) + i128::from(offset),
        SeekFrom::End(offset) => i128::from(size) + i128::from(offset),
    };
    u64::try_from(next)
        .ok()
        .filter(|next| *next <= size)
        .ok_or_else(invalid)
}

impl Seek for RemoteFile {
    fn seek(&mut self, seek: SeekFrom) -> io::Result<u64> {
        self.position = seek_position(self.position, self.identity.size, seek)?;
        Ok(self.position)
    }
}

async fn request(
    client: &Client,
    url: &str,
    start: u64,
    end: u64,
    expected: Option<&ArtifactIdentity>,
) -> io::Result<(ArtifactIdentity, Vec<u8>)> {
    let mut request = client
        .get(url)
        .header(header::RANGE, format!("bytes={start}-{end}"));
    if let Some(expected) = expected {
        request = request.header(header::IF_MATCH, &expected.etag);
    }
    let response = request.send().await.map_err(|error| {
        log_transport_failure(start, end, 0, "request", error);
        io::Error::other("官方 Desktop 制品请求失败")
    })?;
    let status = response.status();
    let invalid_range = |reason: &str, bytes_read: usize, cause: &str| {
        tracing::warn!(
            range_start = start,
            range_end = end,
            bytes_read,
            http_status = status.as_u16(),
            reason,
            cause,
            "Desktop artifact range failed"
        );
        invalid()
    };
    if expected.is_some() && status == StatusCode::PRECONDITION_FAILED {
        return Err(invalid_range(
            "etag_mismatch",
            0,
            "If-Match precondition failed",
        ));
    }
    if status != StatusCode::PARTIAL_CONTENT {
        return Err(invalid_range("http_status", 0, "expected HTTP 206"));
    }
    let range = response
        .headers()
        .get(header::CONTENT_RANGE)
        .and_then(|h| h.to_str().ok())
        .and_then(parse_content_range)
        .ok_or_else(|| invalid_range("invalid_range", 0, "missing or invalid Content-Range"))?;
    let etag = response
        .headers()
        .get(header::ETAG)
        .and_then(|h| h.to_str().ok())
        .filter(|s| s.starts_with('"') && s.ends_with('"') && s.len() <= 128)
        .ok_or_else(|| invalid_range("invalid_etag", 0, "missing or invalid strong ETag"))?
        .to_owned();
    let identity = ArtifactIdentity {
        size: range.2,
        etag,
    };
    if expected.is_some_and(|expected| expected.etag != identity.etag) {
        return Err(invalid_range("etag_mismatch", 0, "response ETag changed"));
    }
    if range.0 != start
        || range.1 != end
        || identity.size == 0
        || identity.size > 2 * 1024 * 1024 * 1024
        || expected.is_some_and(|expected| expected.size != identity.size)
        || response.content_length() != Some(end - start + 1)
    {
        return Err(invalid_range(
            "invalid_range",
            0,
            "range or length mismatch",
        ));
    }
    let length = usize::try_from(end - start + 1).map_err(|_| invalid())?;
    let mut bytes = Vec::with_capacity(length);
    let mut stream = response.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|error| {
            log_transport_failure(start, end, bytes.len(), "body", error);
            io::Error::other("官方 Desktop 制品读取失败")
        })?;
        if bytes.len().saturating_add(chunk.len()) > length {
            return Err(invalid_range(
                "invalid_length",
                bytes.len().saturating_add(chunk.len()),
                "body exceeds range",
            ));
        }
        bytes.extend_from_slice(&chunk);
    }
    if bytes.len() != length {
        return Err(invalid_range(
            "connection_interrupted",
            bytes.len(),
            "body ended before expected length",
        ));
    }
    Ok((identity, bytes))
}

// 仅在运维日志保留底层原因；不打印请求 URL、响应正文或 ETag 值
fn log_transport_failure(
    start: u64,
    end: u64,
    bytes_read: usize,
    phase: &str,
    error: reqwest::Error,
) {
    let error = error.without_url();
    let mut cause: &(dyn Error + 'static) = &error;
    let mut interrupted = false;
    while let Some(source) = cause.source() {
        interrupted |= source.downcast_ref::<io::Error>().is_some_and(|e| {
            matches!(
                e.kind(),
                io::ErrorKind::UnexpectedEof
                    | io::ErrorKind::ConnectionReset
                    | io::ErrorKind::ConnectionAborted
                    | io::ErrorKind::BrokenPipe
            )
        }) || source
            .downcast_ref::<hyper::Error>()
            .is_some_and(|e| e.is_incomplete_message() || e.is_closed())
            || source
                .downcast_ref::<h2::Error>()
                .is_some_and(|e| e.is_reset() || e.is_go_away());
        cause = source;
    }
    let reason = if error.is_timeout() {
        "timeout"
    } else if interrupted {
        "connection_interrupted"
    } else {
        "transport_error"
    };
    tracing::warn!(range_start = start, range_end = end, bytes_read, phase, reason, error = %error, cause = %cause, "Desktop artifact range failed");
}
