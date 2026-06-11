import { createClient } from '@supabase/supabase-js';

const supabaseUrl = 'https://utchzaroqrvhimzsbags.supabase.co';
const supabaseKey = 'REDACTED_SUPABASE_KEY';

export const supabase = createClient(supabaseUrl, supabaseKey);
