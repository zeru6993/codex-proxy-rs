require_relative '../dev-image'

# 普通开发、PR 和定时检查不能获得镜像发布资格
original = ENV.to_h
refs = {
  'refs/heads/release/3.43.1-self.1' => true,
  'refs/heads/release/team/3.43.1' => true,
  'refs/tags/v3.43.1' => true,
  'refs/tags/v3.43.1-self.1' => true,
  'refs/heads/dev' => false,
  'refs/heads/main' => false,
  'refs/heads/feature/test' => false,
  'refs/heads/release' => false,
  'refs/tags/latest' => false,
  'refs/tags/v3.43.1-invalid/' => false,
  'refs/pull/1/merge' => false
}
begin
  refs.each do |ref, allowed|
    %w[push workflow_dispatch pull_request schedule].each do |event|
      ENV['GITHUB_REF'] = ref
      ENV['GITHUB_EVENT_NAME'] = event
      expected = allowed && %w[push workflow_dispatch].include?(event)
      raise "Unexpected publishing permission for #{event} #{ref}" unless DevImage.release_ref? == expected
    end
  end
  puts 'Release ref authorization: 44 cases passed'
ensure
  ENV.replace(original)
end
