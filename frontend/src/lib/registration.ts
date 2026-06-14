import { translate, translateChoiceLabel, type Language } from '@/lib/i18n'

type ChoiceOption = {
  value: string
  label: string
}

export function hasReusableOAuthBrowser(config: { chrome_user_data_dir?: string; chrome_cdp_url?: string }) {
  return Boolean(config.chrome_user_data_dir?.trim() || config.chrome_cdp_url?.trim())
}

function getOptionLabel(value: string, options: ChoiceOption[] = [], language?: Language) {
  return translateChoiceLabel(value, options.find(item => item.value === value)?.label || value, language)
}

export function pickOAuthExecutor(
  supportedExecutors: string[],
  preferredExecutor: string,
  reusableBrowser: boolean,
) {
  if (supportedExecutors.includes(preferredExecutor) && preferredExecutor !== 'protocol') {
    return preferredExecutor
  }
  if (reusableBrowser && supportedExecutors.includes('headless')) {
    return 'headless'
  }
  if (supportedExecutors.includes('headed')) {
    return 'headed'
  }
  if (supportedExecutors.includes('headless')) {
    return 'headless'
  }
  return supportedExecutors[0] || ''
}

export function buildRegistrationOptions(platformMeta: any, language?: Language) {
  const supportedModes: string[] = platformMeta?.supported_identity_modes || []
  const supportedOAuth: string[] = platformMeta?.supported_oauth_providers || []
  const identityModeOptions: ChoiceOption[] = platformMeta?.supported_identity_mode_options || []
  const oauthProviderOptions: ChoiceOption[] = platformMeta?.supported_oauth_provider_options || []
  const options: Array<{
    key: string
    label: string
    description: string
    identityProvider: string
    oauthProvider: string
  }> = []

  if (supportedModes.includes('mailbox')) {
    const label = getOptionLabel('mailbox', identityModeOptions, language)
    options.push({
      key: 'mailbox',
      label,
      description: translate('registration.mailboxDescription', language, { label }),
      identityProvider: 'mailbox',
      oauthProvider: '',
    })
  }

  if (supportedModes.includes('phone')) {
    const label = getOptionLabel('phone', identityModeOptions, language)
    options.push({
      key: 'phone',
      label,
      description: '通过 Hero-SMS 接码注册，无需邮箱',
      identityProvider: 'phone',
      oauthProvider: '',
    })
  }

  if (supportedModes.includes('sms_oauth')) {
    const label = getOptionLabel('sms_oauth', identityModeOptions, language)
    options.push({
      key: 'sms_oauth',
      label: label === 'sms_oauth' ? '\u5148\u624b\u673a\u53f7\u6ce8\u518c OAuth' : label,
      description: '\u624b\u673a\u53f7\u6ce8\u518c + \u7ed1\u5b9a\u90ae\u7bb1 + OAuth \u56de\u8c03\u94fe',
      identityProvider: 'sms_oauth',
      oauthProvider: '',
    })
  }

  if (supportedModes.includes('oauth_browser')) {
    supportedOAuth.forEach((provider: string) => {
      const providerLabel = getOptionLabel(provider, oauthProviderOptions, language)
      options.push({
        key: `oauth:${provider}`,
        label: providerLabel,
        description: translate('registration.oauthDescription', language, { label: providerLabel }),
        identityProvider: 'oauth_browser',
        oauthProvider: provider,
      })
    })
  }

  return options
}

export function buildExecutorOptions(
  identityProvider: string,
  supportedExecutors: string[],
  reusableBrowser: boolean,
  executorOptions: ChoiceOption[] = [],
  language?: Language,
) {
  return supportedExecutors.map((executor) => {
    const option = {
      value: executor,
      label: getOptionLabel(executor, executorOptions, language),
      description: '',
      disabled: false,
      reason: '',
    }

    if (executor === 'protocol') {
      option.description = translate('executor.protocolDescription', language)
      if (identityProvider !== 'mailbox' && identityProvider !== 'phone') {
        option.disabled = true
        option.reason = identityProvider === 'sms_oauth'
          ? '\u5148\u624b\u673a\u53f7\u6ce8\u518c OAuth \u6682\u65f6\u53ea\u652f\u6301\u6d4f\u89c8\u5668\u81ea\u52a8'
          : translate('executor.oauthRequiresBrowser', language)
      }
      return option
    }

    if (executor === 'headless') {
      option.description = identityProvider === 'sms_oauth'
        ? '\u540e\u53f0\u6253\u5f00\u6d4f\u89c8\u5668\uff0c\u81ea\u52a8\u5b8c\u6210\u624b\u673a\u53f7\u6ce8\u518c\u3001\u7ed1\u5b9a\u90ae\u7bb1\u548c OAuth'
        : identityProvider === 'mailbox'
          ? translate('executor.headlessMailboxDescription', language)
          : translate('executor.headlessOauthDescription', language)
      if (identityProvider === 'oauth_browser' && !reusableBrowser) {
        option.disabled = true
        option.reason = translate('executor.requiresChromeProfile', language)
      }
      return option
    }

    option.description = translate('executor.headedDescription', language)
    return option
  })
}
