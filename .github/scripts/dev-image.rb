require 'digest'
require 'fileutils'
require_relative 'release-metadata'

# dev 镜像与部署清单共用源码身份，清单只在质量、安全与运行验收通过后生成
module DevImage
  def self.identity
    sha = ENV.fetch('CPR_GIT_SHA')
    raise 'Expected an exact commit SHA' unless sha.match?(/\A[a-f0-9]{40}\z/) && ReleaseMetadata.git('rev-parse', 'HEAD') == sha
    repository = ENV.fetch('GITHUB_REPOSITORY').downcase
    raise 'Invalid repository' unless repository.match?(/\A[a-z0-9_.-]+\/[a-z0-9_.-]+\z/)
    if ENV['PUBLISH_IMAGE'] == 'true'
      raise 'Only dev push or manual builds may publish' unless ENV['GITHUB_REF'] == 'refs/heads/dev' &&
        %w[push workflow_dispatch].include?(ENV['GITHUB_EVENT_NAME'])
    end
    [sha, repository]
  end

  def self.metadata
    sha, repository = identity
    base = ReleaseMetadata.version(YAML.load_file('release/version.yaml').fetch('version')).split('+', 2).first
    {
      'version' => "#{base}+git.#{sha[0, 12]}",
      'build_time' => Time.now.utc.iso8601,
      'image_tag' => "ghcr.io/#{repository}:sha-#{sha}-#{ENV.fetch('GITHUB_RUN_ID')}-#{ENV.fetch('GITHUB_RUN_ATTEMPT')}"
    }
  end

  def self.deployment
    sha, repository = identity
    digest = ENV.fetch('VERIFIED_IMAGE_DIGEST')
    raise 'Invalid verified image digest' unless digest.match?(/\Asha256:[a-f0-9]{64}\z/)
    version = ReleaseMetadata.version(ENV.fetch('CPR_VERSION'))
    image = "ghcr.io/#{repository}@#{digest}"
    directory = 'dist/deployment'
    FileUtils.mkdir_p(directory)
    manifest = {
      schema_version: 1, repository: repository, git_sha: sha, version: version,
      build_time: ENV.fetch('CPR_BUILD_TIME'), build_type: 'source', platform: 'linux/amd64', image: image,
      workflow_run: "https://github.com/#{repository}/actions/runs/#{ENV.fetch('GITHUB_RUN_ID')}/attempts/#{ENV.fetch('GITHUB_RUN_ATTEMPT')}"
    }
    File.write("#{directory}/deployment.json", JSON.pretty_generate(manifest) + "\n")
    compose = File.read('deploy/compose.yaml')
    default = /\$\{CPR_IMAGE:-ghcr\.io\/[^}]+\}/
    raise 'Expected one application image default' unless compose.scan(default).length == 1
    # 发行附件固定到验收过的摘要，部署时不再解析可变 tag
    File.write("#{directory}/compose.yaml", compose.sub(default, image))
    FileUtils.cp('deploy/config.example.yaml', directory)
    FileUtils.cp('deploy/apply-image.py', directory)
    File.write("#{directory}/summary.md", "源码：`#{sha}`\n\n版本：`#{version}`\n\n镜像：`#{image}`\n\n部署清单与工具见本次运行的 dev-deployment 附件，生产升级执行 `python3 apply-image.py --manifest deployment.json --compose /opt/codex-proxy-rs/deploy/compose.yaml --apply`\n")
    checksums = Dir.glob("#{directory}/*").sort.reject { |p| File.basename(p) == 'checksums.txt' }.map do |path|
      "#{Digest::SHA256.file(path).hexdigest}  #{File.basename(path)}\n"
    end
    File.write("#{directory}/checksums.txt", checksums.join)
  end
end

if $PROGRAM_NAME == __FILE__
  case ARGV
  when ['metadata'] then DevImage.metadata.each { |key, value| puts "#{key}=#{value}" }
  when ['deployment'] then DevImage.deployment
  else abort 'usage: dev-image.rb metadata|deployment'
  end
end
